#include "surge/config.hpp"

#include <charconv>
#include <limits>
#include <string_view>

namespace surge {
namespace {

template <typename T>
bool parse_unsigned(std::string_view text, T& output, bool allow_zero = false) {
  unsigned long long value = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size() ||
      (!allow_zero && value == 0) || value > std::numeric_limits<T>::max()) {
    return false;
  }
  output = static_cast<T>(value);
  return true;
}

bool parse_upstream(std::string_view value, Config& config) {
  std::string_view host;
  std::string_view port;
  if (!value.empty() && value.front() == '[') {
    const auto close = value.find(']');
    if (close == std::string_view::npos || close + 2 > value.size() ||
        value[close + 1] != ':') {
      return false;
    }
    host = value.substr(1, close - 1);
    port = value.substr(close + 2);
  } else {
    const auto colon = value.rfind(':');
    if (colon == std::string_view::npos) return false;
    host = value.substr(0, colon);
    port = value.substr(colon + 1);
  }
  std::uint16_t parsed_port = 0;
  if (host.empty() || !parse_unsigned(port, parsed_port)) return false;
  config.upstream_host.assign(host);
  config.upstream_port = parsed_port;
  return true;
}

}  // namespace

ConfigResult parse_config(int argc, char** argv) {
  ConfigResult result;
  for (int index = 1; index < argc; ++index) {
    const std::string_view option(argv[index]);
    if (option == "--help" || option == "-h") {
      result.show_help = true;
      return result;
    }
    if (index + 1 >= argc) {
      result.error = "missing value for " + std::string(option);
      return result;
    }
    const std::string_view value(argv[++index]);
    bool valid = true;
    if (option == "--listen-address") {
      valid = !value.empty();
      if (valid) result.config.listen_address.assign(value);
    } else if (option == "--listen-port") {
      valid = parse_unsigned(value, result.config.listen_port);
    } else if (option == "--upstream") {
      valid = parse_upstream(value, result.config);
    } else if (option == "--workers") {
      valid = parse_unsigned(value, result.config.workers);
    } else if (option == "--handoff-queue-capacity") {
      valid = parse_unsigned(value, result.config.handoff_queue_capacity);
    } else if (option == "--max-connections") {
      valid = parse_unsigned(value, result.config.max_connections);
    } else if (option == "--max-upstream-connections") {
      valid = parse_unsigned(value, result.config.max_upstream_connections);
    } else if (option == "--max-request-bytes") {
      valid = parse_unsigned(value, result.config.max_request_bytes);
    } else if (option == "--max-response-bytes") {
      valid = parse_unsigned(value, result.config.max_response_bytes);
    } else if (option == "--client-header-timeout-ms") {
      valid = parse_unsigned(value, result.config.client_header_timeout_ms);
    } else if (option == "--upstream-timeout-ms") {
      valid = parse_unsigned(value, result.config.upstream_timeout_ms);
    } else if (option == "--drain-timeout-ms") {
      valid = parse_unsigned(value, result.config.drain_timeout_ms, true);
    } else if (option == "--stats-interval-ms") {
      valid = parse_unsigned(value, result.config.stats_interval_ms, true);
    } else {
      result.error = "unknown option: " + std::string(option);
      return result;
    }
    if (!valid) {
      result.error = "invalid value for " + std::string(option) + ": " + std::string(value);
      return result;
    }
  }
  return result;
}

std::string usage() {
  return R"(usage: surge [options]
  --listen-address ADDRESS             default 0.0.0.0
  --listen-port PORT                   default 8080
  --upstream HOST:PORT                 default 127.0.0.1:9000
  --workers N                          default 1
  --handoff-queue-capacity N           per-worker capacity, default 64
  --max-connections N                  default 1024
  --max-upstream-connections N         default 128
  --max-request-bytes N                default 16384
  --max-response-bytes N               default 1048576
  --client-header-timeout-ms N         default 5000
  --upstream-timeout-ms N              default 10000
  --drain-timeout-ms N                 default 5000
  --stats-interval-ms N                0 disables periodic counters
)";
}

}  // namespace surge
