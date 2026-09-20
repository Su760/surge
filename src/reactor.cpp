#include "surge/reactor.hpp"

#include "surge/bounded_queue.hpp"
#include "surge/http.hpp"
#include "surge/unique_fd.hpp"

#include <arpa/inet.h>
#include <errno.h>
#include <netdb.h>
#include <signal.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/timerfd.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace surge {
namespace {

using Clock = std::chrono::steady_clock;
using Milliseconds = std::chrono::milliseconds;

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

void arm_periodic_timer(int fd) {
  itimerspec timer{};
  timer.it_value.tv_nsec = 50'000'000;
  timer.it_interval.tv_nsec = 50'000'000;
  if (::timerfd_settime(fd, 0, &timer, nullptr) < 0) {
    throw_system_error("timerfd_settime");
  }
}

void consume_counter_fd(int fd) {
  std::uint64_t value = 0;
  while (::read(fd, &value, sizeof(value)) < 0) {
    if (errno == EINTR) continue;
    if (errno == EAGAIN || errno == EWOULDBLOCK) return;
    throw_system_error("read(counter fd)");
  }
}

class CounterLease {
 public:
  CounterLease() noexcept = default;
  ~CounterLease() { reset(); }
  CounterLease(const CounterLease&) = delete;
  CounterLease& operator=(const CounterLease&) = delete;
  CounterLease(CounterLease&& other) noexcept : counter_(std::exchange(other.counter_, nullptr)) {}
  CounterLease& operator=(CounterLease&& other) noexcept {
    if (this != &other) {
      reset();
      counter_ = std::exchange(other.counter_, nullptr);
    }
    return *this;
  }

  static bool try_acquire(std::atomic<std::size_t>& counter, std::size_t limit,
                          CounterLease& output) {
    std::size_t current = counter.load(std::memory_order_relaxed);
    while (current < limit) {
      if (counter.compare_exchange_weak(current, current + 1, std::memory_order_relaxed)) {
        output = CounterLease(&counter);
        return true;
      }
    }
    return false;
  }

  void reset() noexcept {
    if (counter_ != nullptr) {
      counter_->fetch_sub(1, std::memory_order_relaxed);
      counter_ = nullptr;
    }
  }

 private:
  explicit CounterLease(std::atomic<std::size_t>* counter) noexcept : counter_(counter) {}
  std::atomic<std::size_t>* counter_{nullptr};
};

struct SharedState {
  std::atomic<std::size_t> active_connections{0};
  std::atomic<std::size_t> active_upstreams{0};
  std::atomic<std::uint64_t> accepted{0};
  std::atomic<std::uint64_t> completed{0};
  std::atomic<std::uint64_t> rejected{0};
  std::atomic<std::uint64_t> queue_rejected{0};
  std::atomic<std::uint64_t> upstream_errors{0};
  std::atomic<std::uint64_t> timeouts{0};
  std::atomic<std::uint64_t> client_backpressure_events{0};
  std::atomic<std::uint64_t> worker_failures{0};
  std::mutex failure_mutex;
  std::string first_failure;

  void record_worker_failure(std::string message) {
    {
      std::lock_guard lock(failure_mutex);
      if (first_failure.empty()) first_failure = std::move(message);
    }
    worker_failures.fetch_add(1, std::memory_order_relaxed);
  }
};

struct PendingClient {
  UniqueFd socket;
  Clock::time_point accepted_at;
  CounterLease connection_slot;

  PendingClient(UniqueFd client, Clock::time_point accepted, CounterLease slot)
      : socket(std::move(client)), accepted_at(accepted), connection_slot(std::move(slot)) {}
  PendingClient(PendingClient&&) noexcept = default;
  PendingClient& operator=(PendingClient&&) noexcept = default;
  PendingClient(const PendingClient&) = delete;
  PendingClient& operator=(const PendingClient&) = delete;
};

enum class ConnectionState {
  reading_request,
  connecting_upstream,
  writing_upstream,
  reading_upstream,
  writing_client,
};

struct Connection {
  Connection(std::uint64_t connection_id, PendingClient pending,
             std::size_t max_request_bytes, Milliseconds header_timeout)
      : id(connection_id),
        client(std::move(pending.socket)),
        connection_slot(std::move(pending.connection_slot)),
        request_parser(max_request_bytes),
        deadline(pending.accepted_at + header_timeout) {}

  std::uint64_t id;
  UniqueFd client;
  UniqueFd upstream;
  CounterLease connection_slot;
  CounterLease upstream_slot;
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
};

enum class EndpointRole { client, upstream };

struct Endpoint {
  std::uint64_t connection_id;
  EndpointRole role;
};

class Worker {
 public:
  Worker(std::size_t index, const Config& config, ResolvedAddress upstream_address,
         std::string authority, SharedState& shared)
      : index_(index),
        config_(config),
        upstream_address_(upstream_address),
        authority_(std::move(authority)),
        shared_(shared),
        inbox_(config.handoff_queue_capacity),
        epoll_(::epoll_create1(EPOLL_CLOEXEC)),
        wake_fd_(::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC)),
        timer_fd_(::timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC)) {
    if (!epoll_) throw_system_error("epoll_create1(worker)");
    if (!wake_fd_) throw_system_error("eventfd(worker)");
    if (!timer_fd_) throw_system_error("timerfd_create(worker)");
    arm_periodic_timer(timer_fd_.get());
    add_epoll(wake_fd_.get(), kWakeToken, EPOLLIN);
    add_epoll(timer_fd_.get(), kTimerToken, EPOLLIN);
  }

  ~Worker() {
    if (thread_.joinable()) {
      request_force();
      thread_.join();
    }
  }

  Worker(const Worker&) = delete;
  Worker& operator=(const Worker&) = delete;

  void start() { thread_ = std::thread([this] { run_noexcept(); }); }

  bool enqueue(PendingClient& pending) {
    if (!inbox_.try_push(std::move(pending))) return false;
    notify();
    return true;
  }

  void request_drain(Clock::time_point deadline) {
    inbox_.close();
    drain_deadline_ns_.store(to_nanoseconds(deadline), std::memory_order_relaxed);
    drain_requested_.store(true, std::memory_order_release);
    notify();
  }

  void request_force() {
    inbox_.close();
    force_requested_.store(true, std::memory_order_release);
    notify();
  }

  void join() {
    if (thread_.joinable()) thread_.join();
  }

  [[nodiscard]] bool finished() const {
    return finished_.load(std::memory_order_acquire);
  }

  [[nodiscard]] std::uint64_t handled_connections() const {
    return handled_connections_.load(std::memory_order_relaxed);
  }

  [[nodiscard]] std::size_t queued_connections() const { return inbox_.size(); }

 private:
  static constexpr std::uint64_t kWakeToken = 1;
  static constexpr std::uint64_t kTimerToken = 2;

  static std::int64_t to_nanoseconds(Clock::time_point value) {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(value.time_since_epoch()).count();
  }

  static Clock::time_point from_nanoseconds(std::int64_t value) {
    return Clock::time_point(std::chrono::nanoseconds(value));
  }

  void notify() noexcept {
    const std::uint64_t one = 1;
    while (::write(wake_fd_.get(), &one, sizeof(one)) < 0) {
      if (errno == EINTR) continue;
      return;
    }
  }

  void add_epoll(int fd, std::uint64_t token, std::uint32_t events) {
    epoll_event event{};
    event.events = events;
    event.data.u64 = token;
    if (::epoll_ctl(epoll_.get(), EPOLL_CTL_ADD, fd, &event) < 0) {
      throw_system_error("epoll_ctl(ADD worker)");
    }
  }

  void modify_epoll(int fd, std::uint64_t token, std::uint32_t events) {
    epoll_event event{};
    event.events = events;
    event.data.u64 = token;
    if (::epoll_ctl(epoll_.get(), EPOLL_CTL_MOD, fd, &event) < 0 && errno != ENOENT) {
      throw_system_error("epoll_ctl(MOD worker)");
    }
  }

  void remove_epoll(int fd) noexcept {
    if (fd >= 0) ::epoll_ctl(epoll_.get(), EPOLL_CTL_DEL, fd, nullptr);
  }

  void adopt(PendingClient pending) {
    const auto id = next_connection_id_++;
    const int fd = pending.socket.get();
    auto connection = std::make_unique<Connection>(
        id, std::move(pending), config_.max_request_bytes,
        Milliseconds(config_.client_header_timeout_ms));
    connection->client_token = next_event_token_++;
    const auto token = connection->client_token;
    connections_.emplace(id, std::move(connection));
    endpoints_.emplace(token, Endpoint{id, EndpointRole::client});
    try {
      add_epoll(fd, token, EPOLLIN | EPOLLRDHUP);
    } catch (...) {
      close_connection(id);
      throw;
    }
    handled_connections_.fetch_add(1, std::memory_order_relaxed);
  }

  void process_inbox() {
    if (force_requested_.load(std::memory_order_acquire)) {
      force_stopping_ = true;
      draining_ = true;
    } else if (drain_requested_.load(std::memory_order_acquire)) {
      draining_ = true;
      drain_deadline_ = from_nanoseconds(drain_deadline_ns_.load(std::memory_order_relaxed));
    }

    auto pending = inbox_.take_all();
    if (draining_) {
      pending.clear();
    } else {
      for (auto& client : pending) adopt(std::move(client));
    }
    if (force_stopping_) close_all_connections();
  }

  void close_upstream(Connection& connection) noexcept {
    if (connection.upstream) {
      const int fd = connection.upstream.get();
      endpoints_.erase(connection.upstream_token);
      connection.upstream_token = 0;
      remove_epoll(fd);
      connection.upstream.reset();
    }
    connection.upstream_slot.reset();
  }

  void close_connection(std::uint64_t id) noexcept {
    const auto found = connections_.find(id);
    if (found == connections_.end()) return;
    auto& connection = *found->second;
    close_upstream(connection);
    endpoints_.erase(connection.client_token);
    remove_epoll(connection.client.get());
    connections_.erase(found);
  }

  void close_all_connections() noexcept {
    std::vector<std::uint64_t> ids;
    ids.reserve(connections_.size());
    for (const auto& [id, ignored] : connections_) {
      (void)ignored;
      ids.push_back(id);
    }
    for (const auto id : ids) close_connection(id);
  }

  void prepare_error(std::uint64_t id, int status, std::string_view reason,
                     std::string_view detail, bool rejected) {
    const auto found = connections_.find(id);
    if (found == connections_.end()) return;
    auto& connection = *found->second;
    const std::string owned_detail(detail);
    close_upstream(connection);
    connection.response_parser.reset();
    connection.upstream_output.clear();
    connection.client_output = make_error_response(status, reason, owned_detail);
    connection.client_offset = 0;
    connection.state = ConnectionState::writing_client;
    connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
    if (rejected) shared_.rejected.fetch_add(1, std::memory_order_relaxed);
    const std::uint32_t events = EPOLLOUT |
        (connection.client_read_closed ? 0U : EPOLLIN | EPOLLRDHUP);
    modify_epoll(connection.client.get(), connection.client_token, events);
  }

  void start_upstream(std::uint64_t id) {
    const auto found = connections_.find(id);
    if (found == connections_.end()) return;
    auto& connection = *found->second;
    CounterLease upstream_slot;
    if (!CounterLease::try_acquire(shared_.active_upstreams,
                                   config_.max_upstream_connections, upstream_slot)) {
      prepare_error(id, 503, "Service Unavailable", "upstream concurrency limit reached", true);
      return;
    }
    connection.upstream_slot = std::move(upstream_slot);

    UniqueFd socket(::socket(upstream_address_.family,
                             SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, IPPROTO_TCP));
    if (!socket) {
      shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
      prepare_error(id, 502, "Bad Gateway", "could not create upstream socket", false);
      return;
    }
    const int result = ::connect(
        socket.get(), reinterpret_cast<const sockaddr*>(&upstream_address_.storage),
        upstream_address_.length);
    if (result < 0 && errno != EINPROGRESS) {
      shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
      prepare_error(id, 502, "Bad Gateway", "upstream connection failed", false);
      return;
    }

    connection.upstream_output =
        serialize_upstream_request(connection.request_parser.request(), authority_);
    connection.upstream_offset = 0;
    connection.response_parser = std::make_unique<ResponseParser>(config_.max_response_bytes);
    connection.upstream = std::move(socket);
    connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
    connection.state = result == 0 ? ConnectionState::writing_upstream
                                   : ConnectionState::connecting_upstream;
    const int fd = connection.upstream.get();
    connection.upstream_token = next_event_token_++;
    endpoints_.emplace(connection.upstream_token, Endpoint{id, EndpointRole::upstream});
    add_epoll(fd, connection.upstream_token, EPOLLOUT | EPOLLRDHUP);
    modify_epoll(connection.client.get(), connection.client_token, EPOLLIN | EPOLLRDHUP);
  }

  void read_client(std::uint64_t id) {
    std::array<char, 16 * 1024> buffer{};
    while (true) {
      const auto found = connections_.find(id);
      if (found == connections_.end()) return;
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
      shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
      prepare_error(connection.id, 502, "Bad Gateway", "upstream connection failed", false);
      return false;
    }
    connection.state = ConnectionState::writing_upstream;
    return true;
  }

  void write_upstream(std::uint64_t id) {
    while (true) {
      const auto found = connections_.find(id);
      if (found == connections_.end()) return;
      auto& connection = *found->second;
      if (connection.state == ConnectionState::connecting_upstream && !finish_connect(connection)) {
        return;
      }
      const auto remaining = connection.upstream_output.size() - connection.upstream_offset;
      if (remaining == 0) {
        connection.upstream_output.clear();
        connection.state = ConnectionState::reading_upstream;
        connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
        modify_epoll(connection.upstream.get(), connection.upstream_token,
                     EPOLLIN | EPOLLRDHUP);
        return;
      }
      const ssize_t count = ::send(connection.upstream.get(),
                                   connection.upstream_output.data() + connection.upstream_offset,
                                   remaining, MSG_NOSIGNAL);
      if (count > 0) {
        connection.upstream_offset += static_cast<std::size_t>(count);
        connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;
      shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
      prepare_error(id, 502, "Bad Gateway", "upstream write failed", false);
      return;
    }
  }

  void prepare_client_response(std::uint64_t id) {
    const auto found = connections_.find(id);
    if (found == connections_.end()) return;
    auto& connection = *found->second;
    connection.client_output = serialize_client_response(connection.response_parser->response());
    connection.response_parser.reset();
    close_upstream(connection);
    connection.client_offset = 0;
    connection.state = ConnectionState::writing_client;
    connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
    const std::uint32_t events = EPOLLOUT |
        (connection.client_read_closed ? 0U : EPOLLIN | EPOLLRDHUP);
    modify_epoll(connection.client.get(), connection.client_token, events);
  }

  void read_upstream(std::uint64_t id) {
    std::array<char, 32 * 1024> buffer{};
    while (true) {
      const auto found = connections_.find(id);
      if (found == connections_.end()) return;
      auto& connection = *found->second;
      const ssize_t count = ::recv(connection.upstream.get(), buffer.data(), buffer.size(), 0);
      if (count > 0) {
        connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
        const auto state = connection.response_parser->feed(
            std::string_view(buffer.data(), static_cast<std::size_t>(count)));
        if (state == ParseState::complete) {
          prepare_client_response(id);
          return;
        }
        if (state == ParseState::error) {
          shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
          prepare_error(id, 502, "Bad Gateway", connection.response_parser->error(), false);
          return;
        }
        continue;
      }
      if (count == 0) {
        if (connection.response_parser->finish() == ParseState::complete) {
          prepare_client_response(id);
        } else {
          shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
          prepare_error(id, 502, "Bad Gateway", connection.response_parser->error(), false);
        }
        return;
      }
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) return;
      shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
      prepare_error(id, 502, "Bad Gateway", "upstream read failed", false);
      return;
    }
  }

  void write_client(std::uint64_t id) {
    while (true) {
      const auto found = connections_.find(id);
      if (found == connections_.end()) return;
      auto& connection = *found->second;
      const auto remaining = connection.client_output.size() - connection.client_offset;
      if (remaining == 0) {
        shared_.completed.fetch_add(1, std::memory_order_relaxed);
        close_connection(id);
        return;
      }
      const ssize_t count = ::send(connection.client.get(),
                                   connection.client_output.data() + connection.client_offset,
                                   remaining, MSG_NOSIGNAL);
      if (count > 0) {
        connection.client_offset += static_cast<std::size_t>(count);
        connection.deadline = Clock::now() + Milliseconds(config_.upstream_timeout_ms);
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        shared_.client_backpressure_events.fetch_add(1, std::memory_order_relaxed);
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
    const auto found = connections_.find(id);
    if (found != connections_.end() && found->second->state == ConnectionState::writing_client &&
        (events & EPOLLOUT) != 0U) {
      write_client(id);
    }
  }

  void handle_upstream(std::uint64_t id, std::uint32_t events) {
    auto found = connections_.find(id);
    if (found == connections_.end()) return;
    auto state = found->second->state;
    if ((events & EPOLLOUT) != 0U &&
        (state == ConnectionState::connecting_upstream ||
         state == ConnectionState::writing_upstream)) {
      write_upstream(id);
    }
    found = connections_.find(id);
    if (found == connections_.end() || !found->second->upstream) return;
    state = found->second->state;
    if ((events & (EPOLLIN | EPOLLRDHUP | EPOLLHUP)) != 0U &&
        state == ConnectionState::reading_upstream) {
      read_upstream(id);
      return;
    }
    found = connections_.find(id);
    if (found != connections_.end() && found->second->upstream &&
        (events & EPOLLERR) != 0U) {
      shared_.upstream_errors.fetch_add(1, std::memory_order_relaxed);
      prepare_error(id, 502, "Bad Gateway", "upstream socket error", false);
    }
  }

  void handle_timer() {
    consume_counter_fd(timer_fd_.get());
    const auto now = Clock::now();
    std::vector<std::uint64_t> expired;
    for (const auto& [id, connection] : connections_) {
      if (connection->deadline <= now) expired.push_back(id);
    }
    for (const auto id : expired) {
      const auto found = connections_.find(id);
      if (found == connections_.end()) continue;
      shared_.timeouts.fetch_add(1, std::memory_order_relaxed);
      if (found->second->state == ConnectionState::reading_request) {
        prepare_error(id, 408, "Request Timeout", "request headers timed out", true);
      } else if (found->second->state == ConnectionState::writing_client) {
        close_connection(id);
      } else {
        prepare_error(id, 504, "Gateway Timeout", "upstream timed out", false);
      }
    }

    process_inbox();
    if (draining_ && now >= drain_deadline_) {
      force_stopping_ = true;
      close_all_connections();
    }
  }

  void run_loop() {
    std::array<epoll_event, 128> events{};
    process_inbox();
    while (!draining_ || !connections_.empty() || inbox_.size() != 0) {
      const int count = ::epoll_wait(epoll_.get(), events.data(),
                                     static_cast<int>(events.size()), -1);
      if (count < 0) {
        if (errno == EINTR) continue;
        throw_system_error("epoll_wait(worker)");
      }
      for (int index = 0; index < count; ++index) {
        const std::uint64_t token = events[static_cast<std::size_t>(index)].data.u64;
        const std::uint32_t flags = events[static_cast<std::size_t>(index)].events;
        if (token == kWakeToken) {
          consume_counter_fd(wake_fd_.get());
          process_inbox();
          continue;
        }
        if (token == kTimerToken) {
          handle_timer();
          continue;
        }
        const auto endpoint = endpoints_.find(token);
        if (endpoint == endpoints_.end()) continue;
        const Endpoint value = endpoint->second;
        if (value.role == EndpointRole::client) {
          handle_client(value.connection_id, flags);
        } else {
          handle_upstream(value.connection_id, flags);
        }
      }
      process_inbox();
      if (force_stopping_) close_all_connections();
    }
  }

  void run_noexcept() noexcept {
    try {
      run_loop();
    } catch (const std::exception& error) {
      inbox_.close();
      auto pending = inbox_.take_all();
      pending.clear();
      close_all_connections();
      shared_.record_worker_failure("worker " + std::to_string(index_) + ": " + error.what());
    } catch (...) {
      inbox_.close();
      auto pending = inbox_.take_all();
      pending.clear();
      close_all_connections();
      shared_.record_worker_failure("worker " + std::to_string(index_) + ": unknown failure");
    }
    finished_.store(true, std::memory_order_release);
  }

  std::size_t index_;
  Config config_;
  ResolvedAddress upstream_address_;
  std::string authority_;
  SharedState& shared_;
  BoundedQueue<PendingClient> inbox_;
  UniqueFd epoll_;
  UniqueFd wake_fd_;
  UniqueFd timer_fd_;
  std::thread thread_;
  std::unordered_map<std::uint64_t, std::unique_ptr<Connection>> connections_;
  std::unordered_map<std::uint64_t, Endpoint> endpoints_;
  std::uint64_t next_connection_id_{1};
  std::uint64_t next_event_token_{kTimerToken + 1};
  std::atomic<bool> drain_requested_{false};
  std::atomic<bool> force_requested_{false};
  std::atomic<std::int64_t> drain_deadline_ns_{0};
  std::atomic<bool> finished_{false};
  std::atomic<std::uint64_t> handled_connections_{0};
  bool draining_{false};
  bool force_stopping_{false};
  Clock::time_point drain_deadline_{Clock::time_point::max()};
};

void reject_immediately(UniqueFd client, std::string_view detail) {
  const std::string response = make_error_response(503, "Service Unavailable", detail);
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
}

}  // namespace

struct Reactor::Impl {
  static constexpr std::uint64_t kListenerToken = 1;
  static constexpr std::uint64_t kSignalToken = 2;
  static constexpr std::uint64_t kTimerToken = 3;

  explicit Impl(Config value)
      : config(std::move(value)),
        upstream_address(resolve_address(config.upstream_host, config.upstream_port, false)),
        authority(upstream_authority(config)),
        listener(make_listener(config)),
        epoll(::epoll_create1(EPOLL_CLOEXEC)),
        signal_fd(::signalfd(-1, &signal_mask.blocked(), SFD_NONBLOCK | SFD_CLOEXEC)),
        timer_fd(::timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC)) {
    if (!epoll) throw_system_error("epoll_create1(acceptor)");
    if (!signal_fd) throw_system_error("signalfd");
    if (!timer_fd) throw_system_error("timerfd_create(acceptor)");
    arm_periodic_timer(timer_fd.get());
    add_epoll(listener.get(), kListenerToken, EPOLLIN);
    add_epoll(signal_fd.get(), kSignalToken, EPOLLIN);
    add_epoll(timer_fd.get(), kTimerToken, EPOLLIN);
    next_stats = Clock::now() + Milliseconds(config.stats_interval_ms);
  }

  Config config;
  SignalMaskGuard signal_mask;
  SharedState shared;
  ResolvedAddress upstream_address;
  std::string authority;
  UniqueFd listener;
  UniqueFd epoll;
  UniqueFd signal_fd;
  UniqueFd timer_fd;
  std::vector<std::unique_ptr<Worker>> workers;
  std::size_t next_worker{0};
  bool draining{false};
  bool force_stopping{false};
  Clock::time_point drain_deadline{};
  Clock::time_point next_stats{};

  void add_epoll(int fd, std::uint64_t token, std::uint32_t events) {
    epoll_event event{};
    event.events = events;
    event.data.u64 = token;
    if (::epoll_ctl(epoll.get(), EPOLL_CTL_ADD, fd, &event) < 0) {
      throw_system_error("epoll_ctl(ADD acceptor)");
    }
  }

  void remove_epoll(int fd) noexcept {
    if (fd >= 0) ::epoll_ctl(epoll.get(), EPOLL_CTL_DEL, fd, nullptr);
  }

  void start_workers() {
    workers.reserve(config.workers);
    for (std::size_t index = 0; index < config.workers; ++index) {
      workers.push_back(std::make_unique<Worker>(
          index, config, upstream_address, authority, shared));
    }
    try {
      for (auto& worker : workers) worker->start();
    } catch (...) {
      for (auto& worker : workers) worker->request_force();
      for (auto& worker : workers) worker->join();
      throw;
    }
  }

  void print_stats() const {
    std::size_t queued = 0;
    for (const auto& worker : workers) queued += worker->queued_connections();
    std::cerr << "stats accepted=" << shared.accepted.load(std::memory_order_relaxed)
              << " active=" << shared.active_connections.load(std::memory_order_relaxed)
              << " queued=" << queued
              << " upstream_active=" << shared.active_upstreams.load(std::memory_order_relaxed)
              << " completed=" << shared.completed.load(std::memory_order_relaxed)
              << " rejected=" << shared.rejected.load(std::memory_order_relaxed)
              << " queue_rejected=" << shared.queue_rejected.load(std::memory_order_relaxed)
              << " upstream_errors=" << shared.upstream_errors.load(std::memory_order_relaxed)
              << " timeouts=" << shared.timeouts.load(std::memory_order_relaxed)
              << " client_backpressure_events="
              << shared.client_backpressure_events.load(std::memory_order_relaxed)
              << " worker_connections=";
    for (std::size_t index = 0; index < workers.size(); ++index) {
      if (index != 0) std::cerr << ',';
      std::cerr << index << ':' << workers[index]->handled_connections();
    }
    std::cerr << '\n';
  }

  void accept_clients() {
    constexpr std::size_t kAcceptBatch = 128;
    for (std::size_t accepted_in_batch = 0;
         accepted_in_batch < kAcceptBatch && !draining; ++accepted_in_batch) {
      sockaddr_storage address{};
      socklen_t length = sizeof(address);
      UniqueFd client(::accept4(listener.get(), reinterpret_cast<sockaddr*>(&address), &length,
                                SOCK_NONBLOCK | SOCK_CLOEXEC));
      if (!client) {
        if (errno == EINTR) {
          --accepted_in_batch;
          continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) return;
        throw_system_error("accept4");
      }
      shared.accepted.fetch_add(1, std::memory_order_relaxed);

      CounterLease connection_slot;
      if (!CounterLease::try_acquire(shared.active_connections,
                                     config.max_connections, connection_slot)) {
        reject_immediately(std::move(client), "connection limit reached");
        shared.rejected.fetch_add(1, std::memory_order_relaxed);
        continue;
      }

      PendingClient pending(std::move(client), Clock::now(), std::move(connection_slot));
      bool handed_off = false;
      for (std::size_t offset = 0; offset < workers.size(); ++offset) {
        const std::size_t index = (next_worker + offset) % workers.size();
        if (workers[index]->enqueue(pending)) {
          next_worker = (index + 1) % workers.size();
          handed_off = true;
          break;
        }
      }
      if (!handed_off) {
        reject_immediately(std::move(pending.socket), "worker handoff queues are full");
        shared.rejected.fetch_add(1, std::memory_order_relaxed);
        shared.queue_rejected.fetch_add(1, std::memory_order_relaxed);
      }
    }
  }

  void start_draining(bool force) {
    if (!draining) {
      draining = true;
      drain_deadline = Clock::now() + Milliseconds(config.drain_timeout_ms);
      remove_epoll(listener.get());
      listener.reset();
      for (auto& worker : workers) worker->request_drain(drain_deadline);
    }
    if (force && !force_stopping) {
      force_stopping = true;
      for (auto& worker : workers) worker->request_force();
    }
  }

  void handle_signal() {
    signalfd_siginfo info{};
    while (true) {
      const ssize_t count = ::read(signal_fd.get(), &info, sizeof(info));
      if (count == static_cast<ssize_t>(sizeof(info))) {
        start_draining(draining);
        continue;
      }
      if (count < 0 && errno == EINTR) continue;
      if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;
      if (count == 0) return;
      throw_system_error("read(signalfd)");
    }
  }

  bool all_workers_finished() const {
    return std::ranges::all_of(workers, [](const auto& worker) { return worker->finished(); });
  }

  void handle_timer() {
    consume_counter_fd(timer_fd.get());
    const auto now = Clock::now();
    if (config.stats_interval_ms != 0 && now >= next_stats) {
      print_stats();
      next_stats = now + Milliseconds(config.stats_interval_ms);
    }
    if (shared.worker_failures.load(std::memory_order_relaxed) != 0) {
      start_draining(true);
    } else if (draining && now >= drain_deadline) {
      start_draining(true);
    }
  }

  int run() {
    start_workers();
    std::cerr << "surge listening on " << config.listen_address << ':' << config.listen_port
              << " upstream=" << authority << " workers=" << config.workers << '\n';
    std::array<epoll_event, 16> events{};
    while (!draining || !all_workers_finished()) {
      const int count = ::epoll_wait(epoll.get(), events.data(),
                                     static_cast<int>(events.size()), -1);
      if (count < 0) {
        if (errno == EINTR) continue;
        throw_system_error("epoll_wait(acceptor)");
      }
      for (int index = 0; index < count; ++index) {
        const std::uint64_t token = events[static_cast<std::size_t>(index)].data.u64;
        if (listener && token == kListenerToken) {
          accept_clients();
        } else if (token == kSignalToken) {
          handle_signal();
        } else if (token == kTimerToken) {
          handle_timer();
        }
      }
    }
    for (auto& worker : workers) worker->join();
    print_stats();
    const auto failures = shared.worker_failures.load(std::memory_order_relaxed);
    if (failures != 0) {
      std::lock_guard lock(shared.failure_mutex);
      std::cerr << shared.first_failure << '\n';
      return 1;
    }
    return 0;
  }
};

Reactor::Reactor(Config config) : impl_(new Impl(std::move(config))) {}
Reactor::~Reactor() { delete impl_; }
int Reactor::run() { return impl_->run(); }

}  // namespace surge
