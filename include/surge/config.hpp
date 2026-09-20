#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace surge {

struct Config {
  std::string listen_address{"0.0.0.0"};
  std::uint16_t listen_port{8080};
  std::string upstream_host{"127.0.0.1"};
  std::uint16_t upstream_port{9000};
  std::size_t workers{1};
  std::size_t handoff_queue_capacity{64};
  std::size_t max_connections{1024};
  std::size_t max_upstream_connections{128};
  std::size_t max_request_bytes{16 * 1024};
  std::size_t max_response_bytes{1024 * 1024};
  std::uint64_t client_header_timeout_ms{5000};
  std::uint64_t upstream_timeout_ms{10000};
  std::uint64_t drain_timeout_ms{5000};
  std::uint64_t stats_interval_ms{5000};
};

struct ConfigResult {
  Config config;
  bool show_help{false};
  std::string error;
};

ConfigResult parse_config(int argc, char** argv);
std::string usage();

}  // namespace surge
