#include "surge/reactor.hpp"

#include "surge/http.hpp"
#include "surge/unique_fd.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <signal.h>
#include <sys/epoll.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/timerfd.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unordered_map>
#include <utility>
#include <vector>

namespace surge {
namespace {

using Clock = std::chrono::steady_clock;
using Milliseconds = std::chrono::milliseconds;

constexpr std::uint64_t kListenerToken = 1;
constexpr std::uint64_t kSignalToken = 2;
constexpr std::uint64_t kTimerToken = 3;

[[noreturn]] void throw_system_error(std::string_view operation) {
  throw std::system_error(errno, std::generic_category(), std::string(operation));
}

class SignalMaskGuard {
 public:
  SignalMaskGuard() {
    sigemptyset(&blocked_);
    sigaddset(&blocked_, SIGINT);
    sigaddset(&blocked_, SIGTERM);
    if (::sigprocmask(SIG_BLOCK, &blocked_, &previous_) < 0) {
      throw_system_error("sigprocmask");
    }
    active_ = true;
  }

  ~SignalMaskGuard() {
    if (active_) ::sigprocmask(SIG_SETMASK, &previous_, nullptr);
  }

  SignalMaskGuard(const SignalMaskGuard&) = delete;
  SignalMaskGuard& operator=(const SignalMaskGuard&) = delete;
  [[nodiscard]] const sigset_t& blocked() const { return blocked_; }

 private:
  sigset_t blocked_{};
  sigset_t previous_{};
  bool active_{false};
};

struct ResolvedAddress {
  sockaddr_storage storage{};
  socklen_t length{0};
  int family{AF_UNSPEC};
};

ResolvedAddress resolve_address(const std::string& host, std::uint16_t port, bool passive) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_protocol = IPPROTO_TCP;
  hints.ai_flags = passive ? AI_PASSIVE : 0;
  addrinfo* raw = nullptr;
  const std::string service = std::to_string(port);
  const int result = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &raw);
  if (result != 0) {
    throw std::runtime_error("getaddrinfo(" + host + "): " + gai_strerror(result));
  }
  std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> addresses(raw, ::freeaddrinfo);
  for (const addrinfo* current = addresses.get(); current != nullptr; current = current->ai_next) {
    if (current->ai_addrlen > sizeof(sockaddr_storage)) continue;
    ResolvedAddress address;
    std::memcpy(&address.storage, current->ai_addr, current->ai_addrlen);
    address.length = static_cast<socklen_t>(current->ai_addrlen);
    address.family = current->ai_family;
    return address;
  }
  throw std::runtime_error("no usable address for " + host);
}

UniqueFd make_listener(const Config& config) {
  const auto address = resolve_address(config.listen_address, config.listen_port, true);
  UniqueFd socket(::socket(address.family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
                           IPPROTO_TCP));
  if (!socket) throw_system_error("socket(listener)");
  int enabled = 1;
  if (::setsockopt(socket.get(), SOL_SOCKET, SO_REUSEADDR, &enabled, sizeof(enabled)) < 0) {
    throw_system_error("setsockopt(SO_REUSEADDR)");
  }
  if (::bind(socket.get(), reinterpret_cast<const sockaddr*>(&address.storage), address.length) < 0) {
    throw_system_error("bind");
  }
  const auto backlog = static_cast<int>(
      std::min<std::size_t>(config.max_connections, static_cast<std::size_t>(SOMAXCONN)));
  if (::listen(socket.get(), std::max(1, backlog)) < 0) throw_system_error("listen");
  return socket;
}

std::string upstream_authority(const Config& config) {
  const bool ipv6 = config.upstream_host.find(':') != std::string::npos;
  return (ipv6 ? "[" : "") + config.upstream_host + (ipv6 ? "]:" : ":") +
         std::to_string(config.upstream_port);
}

enum class ConnectionState {
  reading_request,
  connecting_upstream,
  writing_upstream,
  reading_upstream,
  writing_client,
};

struct Connection {
  Connection(std::uint64_t connection_id, UniqueFd client_socket,
             std::size_t max_request_bytes, Clock::time_point header_deadline)
      : id(connection_id),
        client(std::move(client_socket)),
        request_parser(max_request_bytes),
        deadline(header_deadline) {}

  std::uint64_t id;
  UniqueFd client;
  UniqueFd upstream;
  RequestParser request_parser;
  std::unique_ptr<ResponseParser> response_parser;
  ConnectionState state{ConnectionState::reading_request};
  std::string upstream_output;
  std::size_t upstream_offset{0};
  std::string client_output;
  std::size_t client_offset{0};
  Clock::time_point deadline;
  std::uint64_t client_token{0};
  std::uint64_t upstream_token{0};
  bool client_read_closed{false};
  bool upstream_counted{false};
};

enum class EndpointRole { client, upstream };

struct Endpoint {
  std::uint64_t connection_id;
  EndpointRole role;
};

struct Counters {
  std::uint64_t accepted{0};
  std::uint64_t completed{0};
  std::uint64_t rejected{0};
  std::uint64_t upstream_errors{0};
  std::uint64_t timeouts{0};
  std::uint64_t client_backpressure_events{0};
};

}  // namespace

struct Reactor::Impl {
  explicit Impl(Config value)
      : config(std::move(value)),
        upstream_address(resolve_address(config.upstream_host, config.upstream_port, false)),
        authority(upstream_authority(config)),
        listener(make_listener(config)),
        epoll(::epoll_create1(EPOLL_CLOEXEC)),
        signal_fd(::signalfd(-1, &signal_mask.blocked(), SFD_NONBLOCK | SFD_CLOEXEC)),
        timer_fd(::timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC)) {
    if (!epoll) throw_system_error("epoll_create1");
    if (!signal_fd) throw_system_error("signalfd");
    if (!timer_fd) throw_system_error("timerfd_create");

    itimerspec timer{};
    timer.it_value.tv_nsec = 50'000'000;
    timer.it_interval.tv_nsec = 50'000'000;
    if (::timerfd_settime(timer_fd.get(), 0, &timer, nullptr) < 0) {
      throw_system_error("timerfd_settime");
    }
    add_epoll(listener.get(), kListenerToken, EPOLLIN);
    add_epoll(signal_fd.get(), kSignalToken, EPOLLIN);
    add_epoll(timer_fd.get(), kTimerToken, EPOLLIN);
    next_stats = Clock::now() + Milliseconds(config.stats_interval_ms);
  }

  Config config;
  SignalMaskGuard signal_mask;
  ResolvedAddress upstream_address;
  std::string authority;
  UniqueFd listener;
  UniqueFd epoll;
  UniqueFd signal_fd;
  UniqueFd timer_fd;
  std::unordered_map<std::uint64_t, std::unique_ptr<Connection>> connections;
  std::unordered_map<std::uint64_t, Endpoint> endpoints;
  Counters counters;
  std::uint64_t next_connection_id{1};
  std::uint64_t next_event_token{kTimerToken + 1};
  std::size_t active_upstreams{0};
  bool draining{false};
  Clock::time_point drain_deadline{};
  Clock::time_point next_stats{};

  void add_epoll(int fd, std::uint64_t token, std::uint32_t events) {
    epoll_event event{};
    event.events = events;
    event.data.u64 = token;
    if (::epoll_ctl(epoll.get(), EPOLL_CTL_ADD, fd, &event) < 0) {
      throw_system_error("epoll_ctl(ADD)");
    }
  }

  void modify_epoll(int fd, std::uint64_t token, std::uint32_t events) {
    epoll_event event{};
    event.events = events;
    event.data.u64 = token;
    if (::epoll_ctl(epoll.get(), EPOLL_CTL_MOD, fd, &event) < 0 && errno != ENOENT) {
      throw_system_error("epoll_ctl(MOD)");
    }
  }

  void remove_epoll(int fd) noexcept {
    if (fd >= 0) ::epoll_ctl(epoll.get(), EPOLL_CTL_DEL, fd, nullptr);
  }

  void print_stats() const {
    std::cerr << "stats accepted=" << counters.accepted
              << " active=" << connections.size()
              << " completed=" << counters.completed
              << " rejected=" << counters.rejected
              << " upstream_errors=" << counters.upstream_errors
              << " timeouts=" << counters.timeouts
              << " client_backpressure_events=" << counters.client_backpressure_events << '\n';
  }

  void close_upstream(Connection& connection) noexcept {
    if (!connection.upstream) return;
    const int fd = connection.upstream.get();
    endpoints.erase(connection.upstream_token);
    connection.upstream_token = 0;
    remove_epoll(fd);
    connection.upstream.reset();
    if (connection.upstream_counted) {
      connection.upstream_counted = false;
      if (active_upstreams > 0) --active_upstreams;
    }
  }

  void close_connection(std::uint64_t id) noexcept {
    const auto found = connections.find(id);
    if (found == connections.end()) return;
    auto& connection = *found->second;
    close_upstream(connection);
    endpoints.erase(connection.client_token);
    remove_epoll(connection.client.get());
    connections.erase(found);
  }

  void prepare_error(std::uint64_t id, int status, std::string_view reason,
                     std::string_view detail, bool rejected) {
    const auto found = connections.find(id);
    if (found == connections.end()) return;
    auto& connection = *found->second;
    const std::string owned_detail(detail);
    close_upstream(connection);
    connection.response_parser.reset();
    connection.upstream_output.clear();
    connection.client_output = make_error_response(status, reason, owned_detail);
    connection.client_offset = 0;
    connection.state = ConnectionState::writing_client;
    connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
    if (rejected) ++counters.rejected;
    const std::uint32_t events = EPOLLOUT |
        (connection.client_read_closed ? 0U : EPOLLIN | EPOLLRDHUP);
    modify_epoll(connection.client.get(), connection.client_token, events);
  }

  void reject_immediately(UniqueFd client) {
    const std::string response =
        make_error_response(503, "Service Unavailable", "connection limit reached");
    ::shutdown(client.get(), SHUT_RD);
    std::size_t offset = 0;
    while (offset < response.size()) {
      const ssize_t count = ::send(client.get(), response.data() + offset,
                                   response.size() - offset, MSG_NOSIGNAL);
      if (count > 0) {
        offset += static_cast<std::size_t>(count);
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      break;
    }
    ::shutdown(client.get(), SHUT_WR);
    ++counters.rejected;
  }

  void accept_clients() {
    while (!draining) {
      sockaddr_storage address{};
      socklen_t length = sizeof(address);
      UniqueFd client(::accept4(listener.get(), reinterpret_cast<sockaddr*>(&address), &length,
                                SOCK_NONBLOCK | SOCK_CLOEXEC));
      if (!client) {
        if (errno == EINTR) continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK) return;
        throw_system_error("accept4");
      }
      ++counters.accepted;
      if (connections.size() >= config.max_connections) {
        reject_immediately(std::move(client));
        continue;
      }
      const auto id = next_connection_id++;
      const int fd = client.get();
      auto connection = std::make_unique<Connection>(
          id, std::move(client), config.max_request_bytes,
          Clock::now() + Milliseconds(config.client_header_timeout_ms));
      connection->client_token = next_event_token++;
      const auto token = connection->client_token;
      connections.emplace(id, std::move(connection));
      endpoints.emplace(token, Endpoint{id, EndpointRole::client});
      add_epoll(fd, token, EPOLLIN | EPOLLRDHUP);
    }
  }

  void start_upstream(std::uint64_t id) {
    const auto found = connections.find(id);
    if (found == connections.end()) return;
    auto& connection = *found->second;
    if (active_upstreams >= config.max_upstream_connections) {
      prepare_error(id, 503, "Service Unavailable", "upstream concurrency limit reached", true);
      return;
    }

    UniqueFd socket(::socket(upstream_address.family,
                             SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, IPPROTO_TCP));
    if (!socket) {
      ++counters.upstream_errors;
      prepare_error(id, 502, "Bad Gateway", "could not create upstream socket", false);
      return;
    }
    const int result = ::connect(
        socket.get(), reinterpret_cast<const sockaddr*>(&upstream_address.storage),
        upstream_address.length);
    if (result < 0 && errno != EINPROGRESS) {
      ++counters.upstream_errors;
      prepare_error(id, 502, "Bad Gateway", "upstream connection failed", false);
      return;
    }

    connection.upstream_output = serialize_upstream_request(connection.request_parser.request(), authority);
    connection.upstream_offset = 0;
    connection.response_parser = std::make_unique<ResponseParser>(config.max_response_bytes);
    connection.upstream = std::move(socket);
    connection.upstream_counted = true;
    ++active_upstreams;
    connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
    connection.state = result == 0 ? ConnectionState::writing_upstream
                                   : ConnectionState::connecting_upstream;
    const int fd = connection.upstream.get();
    connection.upstream_token = next_event_token++;
    endpoints.emplace(connection.upstream_token, Endpoint{id, EndpointRole::upstream});
    add_epoll(fd, connection.upstream_token, EPOLLOUT | EPOLLRDHUP);
    modify_epoll(connection.client.get(), connection.client_token, EPOLLIN | EPOLLRDHUP);
  }

  void read_client(std::uint64_t id) {
    std::array<char, 16 * 1024> buffer{};
    while (true) {
      const auto found = connections.find(id);
      if (found == connections.end()) return;
      auto& connection = *found->second;
      const ssize_t count = ::recv(connection.client.get(), buffer.data(), buffer.size(), 0);
      if (count > 0) {
        if (connection.state != ConnectionState::reading_request) {
          prepare_error(id, 400, "Bad Request", "pipelining is unsupported", true);
          return;
        }
        const auto state = connection.request_parser.feed(
            std::string_view(buffer.data(), static_cast<std::size_t>(count)));
        if (state == ParseState::error) {
          const bool oversized = connection.request_parser.error().find("buffer limit") !=
                                 std::string::npos;
          prepare_error(id, oversized ? 431 : 400,
                        oversized ? "Request Header Fields Too Large" : "Bad Request",
                        connection.request_parser.error(), true);
          return;
        }
        if (state == ParseState::complete) start_upstream(id);
        continue;
      }
      if (count == 0) {
        if (connection.state == ConnectionState::reading_request) {
          close_connection(id);
        } else {
          connection.client_read_closed = true;
          const std::uint32_t events = connection.state == ConnectionState::writing_client
                                           ? EPOLLOUT
                                           : 0U;
          modify_epoll(connection.client.get(), connection.client_token, events);
        }
        return;
      }
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) return;
      close_connection(id);
      return;
    }
  }

  bool finish_connect(Connection& connection) {
    int error = 0;
    socklen_t length = sizeof(error);
    if (::getsockopt(connection.upstream.get(), SOL_SOCKET, SO_ERROR, &error, &length) < 0) {
      error = errno;
    }
    if (error != 0) {
      ++counters.upstream_errors;
      prepare_error(connection.id, 502, "Bad Gateway", "upstream connection failed", false);
      return false;
    }
    connection.state = ConnectionState::writing_upstream;
    return true;
  }

  void write_upstream(std::uint64_t id) {
    while (true) {
      const auto found = connections.find(id);
      if (found == connections.end()) return;
      auto& connection = *found->second;
      if (connection.state == ConnectionState::connecting_upstream && !finish_connect(connection)) {
        return;
      }
      const auto remaining = connection.upstream_output.size() - connection.upstream_offset;
      if (remaining == 0) {
        connection.upstream_output.clear();
        connection.state = ConnectionState::reading_upstream;
        connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
        modify_epoll(connection.upstream.get(), connection.upstream_token,
                     EPOLLIN | EPOLLRDHUP);
        return;
      }
      const ssize_t count = ::send(connection.upstream.get(),
                                   connection.upstream_output.data() + connection.upstream_offset,
                                   remaining, MSG_NOSIGNAL);
      if (count > 0) {
        connection.upstream_offset += static_cast<std::size_t>(count);
        connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;
      ++counters.upstream_errors;
      prepare_error(id, 502, "Bad Gateway", "upstream write failed", false);
      return;
    }
  }

  void prepare_client_response(std::uint64_t id) {
    const auto found = connections.find(id);
    if (found == connections.end()) return;
    auto& connection = *found->second;
    connection.client_output = serialize_client_response(connection.response_parser->response());
    connection.response_parser.reset();
    close_upstream(connection);
    connection.client_offset = 0;
    connection.state = ConnectionState::writing_client;
    connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
    const std::uint32_t events = EPOLLOUT |
        (connection.client_read_closed ? 0U : EPOLLIN | EPOLLRDHUP);
    modify_epoll(connection.client.get(), connection.client_token, events);
  }

  void read_upstream(std::uint64_t id) {
    std::array<char, 32 * 1024> buffer{};
    while (true) {
      const auto found = connections.find(id);
      if (found == connections.end()) return;
      auto& connection = *found->second;
      const ssize_t count = ::recv(connection.upstream.get(), buffer.data(), buffer.size(), 0);
      if (count > 0) {
        connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
        const auto state = connection.response_parser->feed(
            std::string_view(buffer.data(), static_cast<std::size_t>(count)));
        if (state == ParseState::complete) {
          prepare_client_response(id);
          return;
        }
        if (state == ParseState::error) {
          ++counters.upstream_errors;
          prepare_error(id, 502, "Bad Gateway", connection.response_parser->error(), false);
          return;
        }
        continue;
      }
      if (count == 0) {
        if (connection.response_parser->finish() == ParseState::complete) {
          prepare_client_response(id);
        } else {
          ++counters.upstream_errors;
          prepare_error(id, 502, "Bad Gateway", connection.response_parser->error(), false);
        }
        return;
      }
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) return;
      ++counters.upstream_errors;
      prepare_error(id, 502, "Bad Gateway", "upstream read failed", false);
      return;
    }
  }

  void write_client(std::uint64_t id) {
    while (true) {
      const auto found = connections.find(id);
      if (found == connections.end()) return;
      auto& connection = *found->second;
      const auto remaining = connection.client_output.size() - connection.client_offset;
      if (remaining == 0) {
        ++counters.completed;
        close_connection(id);
        return;
      }
      const ssize_t count = ::send(connection.client.get(),
                                   connection.client_output.data() + connection.client_offset,
                                   remaining, MSG_NOSIGNAL);
      if (count > 0) {
        connection.client_offset += static_cast<std::size_t>(count);
        connection.deadline = Clock::now() + Milliseconds(config.upstream_timeout_ms);
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        ++counters.client_backpressure_events;
        return;
      }
      close_connection(id);
      return;
    }
  }

  void handle_client(std::uint64_t id, std::uint32_t events) {
    if ((events & EPOLLERR) != 0U) {
      close_connection(id);
      return;
    }
    if ((events & (EPOLLIN | EPOLLRDHUP | EPOLLHUP)) != 0U) read_client(id);
    if ((events & EPOLLHUP) != 0U) {
      close_connection(id);
      return;
    }
    const auto found = connections.find(id);
    if (found != connections.end() && found->second->state == ConnectionState::writing_client &&
        (events & EPOLLOUT) != 0U) {
      write_client(id);
    }
  }

  void handle_upstream(std::uint64_t id, std::uint32_t events) {
    auto found = connections.find(id);
    if (found == connections.end()) return;
    auto state = found->second->state;
    if ((events & EPOLLOUT) != 0U &&
        (state == ConnectionState::connecting_upstream ||
         state == ConnectionState::writing_upstream)) {
      write_upstream(id);
    }
    found = connections.find(id);
    if (found == connections.end() || !found->second->upstream) return;
    state = found->second->state;
    if ((events & (EPOLLIN | EPOLLRDHUP | EPOLLHUP)) != 0U &&
        state == ConnectionState::reading_upstream) {
      read_upstream(id);
      return;
    }
    found = connections.find(id);
    if (found != connections.end() && found->second->upstream &&
        (events & EPOLLERR) != 0U) {
      ++counters.upstream_errors;
      prepare_error(id, 502, "Bad Gateway", "upstream socket error", false);
    }
  }

  void start_draining() {
    if (draining) {
      std::vector<std::uint64_t> ids;
      ids.reserve(connections.size());
      for (const auto& [id, ignored] : connections) {
        (void)ignored;
        ids.push_back(id);
      }
      for (const auto id : ids) close_connection(id);
      return;
    }
    draining = true;
    drain_deadline = Clock::now() + Milliseconds(config.drain_timeout_ms);
    remove_epoll(listener.get());
    listener.reset();
  }

  void handle_signal() {
    signalfd_siginfo info{};
    while (true) {
      const ssize_t count = ::read(signal_fd.get(), &info, sizeof(info));
      if (count == static_cast<ssize_t>(sizeof(info))) {
        start_draining();
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;
      if (count == 0) return;
      throw_system_error("read(signalfd)");
    }
  }

  void handle_timer() {
    std::uint64_t expirations = 0;
    while (::read(timer_fd.get(), &expirations, sizeof(expirations)) < 0 && errno == EINTR) {
    }
    const auto now = Clock::now();
    std::vector<std::uint64_t> expired;
    for (const auto& [id, connection] : connections) {
      if (connection->deadline <= now) expired.push_back(id);
    }
    for (const auto id : expired) {
      const auto found = connections.find(id);
      if (found == connections.end()) continue;
      ++counters.timeouts;
      if (found->second->state == ConnectionState::reading_request) {
        prepare_error(id, 408, "Request Timeout", "request headers timed out", true);
      } else if (found->second->state == ConnectionState::writing_client) {
        close_connection(id);
      } else {
        prepare_error(id, 504, "Gateway Timeout", "upstream timed out", false);
      }
    }

    if (config.stats_interval_ms != 0 && now >= next_stats) {
      print_stats();
      next_stats = now + Milliseconds(config.stats_interval_ms);
    }
    if (draining && now >= drain_deadline) {
      std::vector<std::uint64_t> ids;
      ids.reserve(connections.size());
      for (const auto& [id, ignored] : connections) {
        (void)ignored;
        ids.push_back(id);
      }
      for (const auto id : ids) close_connection(id);
    }
  }

  int run() {
    std::cerr << "surge listening on " << config.listen_address << ':' << config.listen_port
              << " upstream=" << authority << '\n';
    std::array<epoll_event, 128> events{};
    while (!draining || !connections.empty()) {
      const int count = ::epoll_wait(epoll.get(), events.data(),
                                     static_cast<int>(events.size()), -1);
      if (count < 0) {
        if (errno == EINTR) continue;
        throw_system_error("epoll_wait");
      }
      for (int index = 0; index < count; ++index) {
        const std::uint64_t token = events[static_cast<std::size_t>(index)].data.u64;
        const std::uint32_t flags = events[static_cast<std::size_t>(index)].events;
        if (listener && token == kListenerToken) {
          accept_clients();
          continue;
        }
        if (token == kSignalToken) {
          handle_signal();
          continue;
        }
        if (token == kTimerToken) {
          handle_timer();
          continue;
        }
        const auto endpoint = endpoints.find(token);
        if (endpoint == endpoints.end()) continue;
        const Endpoint value = endpoint->second;
        if (value.role == EndpointRole::client) {
          handle_client(value.connection_id, flags);
        } else {
          handle_upstream(value.connection_id, flags);
        }
      }
    }
    print_stats();
    return 0;
  }
};

Reactor::Reactor(Config config) : impl_(new Impl(std::move(config))) {}
Reactor::~Reactor() { delete impl_; }
int Reactor::run() { return impl_->run(); }

}  // namespace surge
