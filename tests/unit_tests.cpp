#include "surge/bounded_queue.hpp"
#include "surge/config.hpp"
#include "surge/http.hpp"
#include "surge/unique_fd.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <cstdlib>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

extern "C" int __real_close(int fd);

namespace {

int close_eintr_target = -1;
int close_replacement_source = -1;
bool close_eintr_injected = false;

}  // namespace

extern "C" int __wrap_close(int fd) {
  if (fd == close_eintr_target) {
    close_eintr_target = -1;
    const int result = __real_close(fd);
    if (result == 0 && ::dup2(close_replacement_source, fd) == fd) {
      close_eintr_injected = true;
      errno = EINTR;
      return -1;
    }
    return result;
  }
  return __real_close(fd);
}

namespace {

int failures = 0;

#define CHECK(condition)                                                        \
  do {                                                                          \
    if (!(condition)) {                                                          \
      std::cerr << __FILE__ << ':' << __LINE__ << ": CHECK failed: "           \
                << #condition << '\n';                                           \
      ++failures;                                                                \
    }                                                                            \
  } while (false)

void test_unique_fd_closes_and_moves() {
  int pipe_fds[2]{};
  CHECK(::pipe(pipe_fds) == 0);
  const int owned = pipe_fds[0];
  {
    surge::UniqueFd first(owned);
    surge::UniqueFd second(std::move(first));
    CHECK(!first);
    CHECK(second.get() == owned);
  }
  CHECK(::fcntl(owned, F_GETFD) == -1);
  ::close(pipe_fds[1]);
}

void test_unique_fd_does_not_retry_close_after_eintr() {
  int pipe_fds[2]{};
  CHECK(::pipe(pipe_fds) == 0);
  close_eintr_target = pipe_fds[0];
  close_replacement_source = pipe_fds[1];
  close_eintr_injected = false;
  {
    surge::UniqueFd owned(pipe_fds[0]);
  }
  CHECK(close_eintr_injected);
  CHECK(::fcntl(pipe_fds[0], F_GETFD) != -1);
  CHECK(__real_close(pipe_fds[0]) == 0);
  CHECK(::close(pipe_fds[1]) == 0);
  close_replacement_source = -1;
}

void test_unique_fd_reset_with_same_descriptor_keeps_ownership() {
  int pipe_fds[2]{};
  CHECK(::pipe(pipe_fds) == 0);
  {
    surge::UniqueFd owned(pipe_fds[0]);
    owned.reset(owned.get());
    CHECK(::fcntl(pipe_fds[0], F_GETFD) != -1);
  }
  CHECK(::fcntl(pipe_fds[0], F_GETFD) == -1);
  CHECK(::close(pipe_fds[1]) == 0);
}

void test_handoff_queue_saturation_and_shutdown_cleanup() {
  int first_pipe[2]{};
  int second_pipe[2]{};
  CHECK(::pipe(first_pipe) == 0);
  CHECK(::pipe(second_pipe) == 0);
  surge::BoundedQueue<surge::UniqueFd> queue(1);
  surge::UniqueFd first(first_pipe[0]);
  surge::UniqueFd second(second_pipe[0]);
  CHECK(queue.try_push(std::move(first)));
  CHECK(!first);
  CHECK(!queue.try_push(std::move(second)));
  CHECK(second.get() == second_pipe[0]);
  queue.close();
  CHECK(!queue.try_push(std::move(second)));
  CHECK(second.get() == second_pipe[0]);
  auto batch = queue.take_all();
  CHECK(batch.size() == 1);
  CHECK(batch.front().get() == first_pipe[0]);
  batch.clear();
  CHECK(::fcntl(first_pipe[0], F_GETFD) == -1);
  second.reset();
  CHECK(::close(first_pipe[1]) == 0);
  CHECK(::close(second_pipe[1]) == 0);
}

void test_config_parses_limits_and_rejects_invalid_values() {
  std::vector<std::string> values{
      "surge", "--listen-port", "18080", "--upstream", "127.0.0.1:19000",
      "--workers", "4", "--handoff-queue-capacity", "9",
      "--max-connections", "7", "--max-upstream-connections", "3",
      "--max-request-bytes", "2048", "--max-response-bytes", "4096",
      "--client-header-timeout-ms", "250", "--upstream-timeout-ms", "500",
      "--drain-timeout-ms", "750", "--stats-interval-ms", "0"};
  std::vector<char*> argv;
  for (auto& value : values) argv.push_back(value.data());
  const auto result = surge::parse_config(static_cast<int>(argv.size()), argv.data());
  CHECK(result.error.empty());
  CHECK(result.config.listen_port == 18080);
  CHECK(result.config.upstream_port == 19000);
  CHECK(result.config.workers == 4);
  CHECK(result.config.handoff_queue_capacity == 9);
  CHECK(result.config.max_connections == 7);
  CHECK(result.config.max_upstream_connections == 3);
  CHECK(result.config.max_request_bytes == 2048);
  CHECK(result.config.max_response_bytes == 4096);
  CHECK(result.config.client_header_timeout_ms == 250);
  CHECK(result.config.upstream_timeout_ms == 500);
  CHECK(result.config.drain_timeout_ms == 750);
  CHECK(result.config.stats_interval_ms == 0);

  char arg0[] = "surge";
  char arg1[] = "--max-connections";
  char arg2[] = "0";
  char* bad_argv[]{arg0, arg1, arg2};
  CHECK(!surge::parse_config(3, bad_argv).error.empty());
}

void test_fragmented_request_and_hop_by_hop_filtering() {
  surge::RequestParser parser(4096);
  CHECK(parser.feed("GET /fast HTTP/1.1\r\nHo") == surge::ParseState::in_progress);
  CHECK(parser.feed("st: client.example\r\nConnection: keep-alive, X-Remove\r\n") ==
        surge::ParseState::in_progress);
  CHECK(parser.feed("X-Remove: secret\r\nX-Keep: yes\r\n\r\n") ==
        surge::ParseState::complete);
  const auto wire = surge::serialize_upstream_request(parser.request(), "127.0.0.1:9000");
  CHECK(wire == "GET /fast HTTP/1.1\r\nHost: 127.0.0.1:9000\r\n"
                "X-Keep: yes\r\nConnection: close\r\n\r\n");
}

void test_request_rejects_unsupported_or_ambiguous_framing() {
  for (const std::string& wire : {
           "POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
           "GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\n\r\nx",
           "GET / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n",
           "GET / HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\n\r\n",
           "GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n"}) {
    surge::RequestParser parser(4096);
    CHECK(parser.feed(wire) == surge::ParseState::error);
    CHECK(!parser.error().empty());
  }
}

void test_fragmented_content_length_response() {
  surge::ResponseParser parser(4096);
  CHECK(parser.feed("HTTP/1.1 200 OK\r\nContent-Len") == surge::ParseState::in_progress);
  CHECK(parser.feed("gth: 5\r\nConnection: keep-alive\r\nX-Test: ok\r\n\r\nhe") ==
        surge::ParseState::in_progress);
  CHECK(parser.feed("llo") == surge::ParseState::complete);
  CHECK(surge::serialize_client_response(parser.response()) ==
        "HTTP/1.1 200 OK\r\nContent-Length: 5\r\nX-Test: ok\r\n"
        "Connection: close\r\n\r\nhello");
}

void test_response_requires_unambiguous_content_length_and_limit() {
  for (const std::string& wire : {
           "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhello",
           "HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\nx",
           "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"}) {
    surge::ResponseParser parser(4096);
    CHECK(parser.feed(wire) == surge::ParseState::error);
  }

  surge::ResponseParser too_large(64);
  CHECK(too_large.feed("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n") ==
        surge::ParseState::error);
}

}  // namespace

int main() {
  test_unique_fd_closes_and_moves();
  test_unique_fd_does_not_retry_close_after_eintr();
  test_unique_fd_reset_with_same_descriptor_keeps_ownership();
  test_handoff_queue_saturation_and_shutdown_cleanup();
  test_config_parses_limits_and_rejects_invalid_values();
  test_fragmented_request_and_hop_by_hop_filtering();
  test_request_rejects_unsupported_or_ambiguous_framing();
  test_fragmented_content_length_response();
  test_response_requires_unambiguous_content_length_and_limit();
  if (failures != 0) {
    std::cerr << failures << " test assertion(s) failed\n";
    return EXIT_FAILURE;
  }
  std::cout << "all unit tests passed\n";
  return EXIT_SUCCESS;
}
