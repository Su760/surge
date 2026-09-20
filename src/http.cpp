#include "surge/http.hpp"

#include <llhttp.h>

#include <algorithm>
#include <charconv>
#include <cctype>
#include <limits>
#include <optional>
#include <sstream>
#include <unordered_set>

namespace surge {
namespace {

std::string lower(std::string_view value) {
  std::string result;
  result.reserve(value.size());
  for (const char character : value) {
    result.push_back(static_cast<char>(
        std::tolower(static_cast<unsigned char>(character))));
  }
  return result;
}

std::string_view trim(std::string_view value) {
  while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) {
    value.remove_prefix(1);
  }
  while (!value.empty() && (value.back() == ' ' || value.back() == '\t')) {
    value.remove_suffix(1);
  }
  return value;
}

std::unordered_set<std::string> connection_tokens(
    const std::vector<std::pair<std::string, std::string>>& headers) {
  std::unordered_set<std::string> result;
  for (const auto& [name, value] : headers) {
    if (lower(name) != "connection") continue;
    std::string_view remaining(value);
    while (!remaining.empty()) {
      const auto comma = remaining.find(',');
      const auto token = trim(remaining.substr(0, comma));
      if (!token.empty()) result.insert(lower(token));
      if (comma == std::string_view::npos) break;
      remaining.remove_prefix(comma + 1);
    }
  }
  return result;
}

bool standard_hop_header(std::string_view name) {
  const auto normalized = lower(name);
  return normalized == "connection" || normalized == "keep-alive" ||
         normalized == "proxy-authenticate" || normalized == "proxy-authorization" ||
         normalized == "te" || normalized == "trailer" ||
         normalized == "transfer-encoding" || normalized == "upgrade" ||
         normalized == "proxy-connection";
}

std::optional<std::size_t> decimal_size(std::string_view text) {
  text = trim(text);
  std::size_t value = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size()) return std::nullopt;
  return value;
}

template <typename Message>
struct ParserStorage {
  llhttp_t parser{};
  llhttp_settings_t settings{};
  Message message;
  std::string field;
  std::string value;
  std::string error;
  std::size_t max_bytes;
  std::size_t seen_bytes{0};
  bool complete{false};

  explicit ParserStorage(std::size_t limit) : max_bytes(limit) {}

  static ParserStorage& self(llhttp_t* parser) {
    return *static_cast<ParserStorage*>(parser->data);
  }

  static int on_header_field(llhttp_t* parser, const char* at, std::size_t length) {
    self(parser).field.append(at, length);
    return 0;
  }

  static int on_header_value(llhttp_t* parser, const char* at, std::size_t length) {
    self(parser).value.append(at, length);
    return 0;
  }

  static int on_header_value_complete(llhttp_t* parser) {
    auto& storage = self(parser);
    storage.message.headers.emplace_back(std::move(storage.field), std::move(storage.value));
    storage.field.clear();
    storage.value.clear();
    return 0;
  }

  static int on_message_complete(llhttp_t* parser) {
    self(parser).complete = true;
    return 0;
  }

  static int on_reset(llhttp_t* parser) {
    self(parser).error = "pipelining is unsupported";
    return -1;
  }

  ParseState execute(std::string_view bytes) {
    if (!error.empty()) return ParseState::error;
    if (complete && !bytes.empty()) {
      error = "bytes after the single HTTP message";
      return ParseState::error;
    }
    if (bytes.size() > max_bytes - std::min(max_bytes, seen_bytes)) {
      error = "HTTP message exceeds configured buffer limit";
      return ParseState::error;
    }
    seen_bytes += bytes.size();
    const auto code = llhttp_execute(&parser, bytes.data(), bytes.size());
    if (code != HPE_OK) {
      if (error.empty()) {
        const char* reason = llhttp_get_error_reason(&parser);
        error = reason == nullptr ? llhttp_errno_name(code) : reason;
      }
      return ParseState::error;
    }
    return complete ? ParseState::complete : ParseState::in_progress;
  }
};

bool has_header(const std::vector<std::pair<std::string, std::string>>& headers,
                std::string_view wanted) {
  return std::ranges::any_of(headers, [&](const auto& header) {
    return lower(header.first) == wanted;
  });
}

std::vector<std::string_view> header_values(
    const std::vector<std::pair<std::string, std::string>>& headers,
    std::string_view wanted) {
  std::vector<std::string_view> result;
  for (const auto& [name, value] : headers) {
    if (lower(name) == wanted) result.push_back(value);
  }
  return result;
}

}  // namespace

struct RequestParser::Impl : ParserStorage<HttpRequest> {
  explicit Impl(std::size_t limit) : ParserStorage(limit) {
    llhttp_settings_init(&settings);
    settings.on_url = [](llhttp_t* context, const char* at, std::size_t length) {
      self(context).message.target.append(at, length);
      return 0;
    };
    settings.on_header_field = on_header_field;
    settings.on_header_value = on_header_value;
    settings.on_header_value_complete = on_header_value_complete;
    settings.on_headers_complete = [](llhttp_t* context) {
      auto& storage = self(context);
      if (llhttp_get_method(context) != HTTP_GET) {
        storage.error = "only GET is supported";
        return -1;
      }
      if (llhttp_get_http_major(context) != 1 || llhttp_get_http_minor(context) != 1) {
        storage.error = "only HTTP/1.1 is supported";
        return -1;
      }
      if (storage.message.target.empty() || storage.message.target.front() != '/') {
        storage.error = "only origin-form request targets are supported";
        return -1;
      }
      const auto hosts = header_values(storage.message.headers, "host");
      if (hosts.size() != 1 || trim(hosts.front()).empty()) {
        storage.error = "exactly one Host header is required";
        return -1;
      }
      if (has_header(storage.message.headers, "content-length") ||
          has_header(storage.message.headers, "transfer-encoding")) {
        storage.error = "request bodies and body framing are unsupported";
        return -1;
      }
      if (has_header(storage.message.headers, "expect")) {
        storage.error = "Expect is unsupported";
        return -1;
      }
      if (has_header(storage.message.headers, "upgrade") || llhttp_get_upgrade(context) != 0) {
        storage.error = "protocol upgrades are unsupported";
        return -1;
      }
      const auto tokens = connection_tokens(storage.message.headers);
      if (tokens.contains("host") || tokens.contains("content-length") ||
          tokens.contains("transfer-encoding")) {
        storage.error = "Connection nominates a framing header";
        return -1;
      }
      return 1;
    };
    settings.on_message_complete = on_message_complete;
    settings.on_reset = on_reset;
    llhttp_init(&parser, HTTP_REQUEST, &settings);
    parser.data = this;
  }
};

RequestParser::RequestParser(std::size_t max_bytes) : impl_(std::make_unique<Impl>(max_bytes)) {}
RequestParser::~RequestParser() = default;
RequestParser::RequestParser(RequestParser&&) noexcept = default;
RequestParser& RequestParser::operator=(RequestParser&&) noexcept = default;
ParseState RequestParser::feed(std::string_view bytes) { return impl_->execute(bytes); }
const HttpRequest& RequestParser::request() const { return impl_->message; }
const std::string& RequestParser::error() const { return impl_->error; }

struct ResponseParser::Impl : ParserStorage<HttpResponse> {
  explicit Impl(std::size_t limit) : ParserStorage(limit) {
    llhttp_settings_init(&settings);
    settings.on_status = [](llhttp_t* context, const char* at, std::size_t length) {
      self(context).message.reason.append(at, length);
      return 0;
    };
    settings.on_header_field = on_header_field;
    settings.on_header_value = on_header_value;
    settings.on_header_value_complete = on_header_value_complete;
    settings.on_headers_complete = [](llhttp_t* context) {
      auto& storage = self(context);
      storage.message.status_code = llhttp_get_status_code(context);
      if (llhttp_get_http_major(context) != 1 || llhttp_get_http_minor(context) != 1) {
        storage.error = "upstream response is not HTTP/1.1";
        return -1;
      }
      if (storage.message.status_code < 200) {
        storage.error = "informational upstream responses are unsupported";
        return -1;
      }
      if (has_header(storage.message.headers, "transfer-encoding")) {
        storage.error = "chunked upstream responses are unsupported";
        return -1;
      }
      const auto lengths = header_values(storage.message.headers, "content-length");
      if (lengths.size() != 1) {
        storage.error = "exactly one upstream Content-Length is required";
        return -1;
      }
      const auto length = decimal_size(lengths.front());
      if (!length.has_value() || *length > storage.max_bytes) {
        storage.error = "invalid or oversized upstream Content-Length";
        return -1;
      }
      const auto tokens = connection_tokens(storage.message.headers);
      if (tokens.contains("content-length") || tokens.contains("transfer-encoding")) {
        storage.error = "Connection nominates a framing header";
        return -1;
      }
      return 0;
    };
    settings.on_body = [](llhttp_t* context, const char* at, std::size_t length) {
      self(context).message.body.append(at, length);
      return 0;
    };
    settings.on_message_complete = on_message_complete;
    settings.on_reset = on_reset;
    llhttp_init(&parser, HTTP_RESPONSE, &settings);
    parser.data = this;
  }
};

ResponseParser::ResponseParser(std::size_t max_bytes)
    : impl_(std::make_unique<Impl>(max_bytes)) {}
ResponseParser::~ResponseParser() = default;
ResponseParser::ResponseParser(ResponseParser&&) noexcept = default;
ResponseParser& ResponseParser::operator=(ResponseParser&&) noexcept = default;
ParseState ResponseParser::feed(std::string_view bytes) { return impl_->execute(bytes); }
ParseState ResponseParser::finish() {
  if (!impl_->error.empty()) return ParseState::error;
  const auto code = llhttp_finish(&impl_->parser);
  if (code != HPE_OK || !impl_->complete) {
    const char* reason = llhttp_get_error_reason(&impl_->parser);
    impl_->error = reason == nullptr ? "truncated upstream response" : reason;
    return ParseState::error;
  }
  return ParseState::complete;
}
const HttpResponse& ResponseParser::response() const { return impl_->message; }
const std::string& ResponseParser::error() const { return impl_->error; }

std::string serialize_upstream_request(const HttpRequest& request,
                                       std::string_view upstream_authority) {
  const auto tokens = connection_tokens(request.headers);
  std::ostringstream output;
  output << "GET " << request.target << " HTTP/1.1\r\nHost: " << upstream_authority << "\r\n";
  for (const auto& [name, value] : request.headers) {
    const auto normalized = lower(name);
    if (normalized == "host" || standard_hop_header(name) || tokens.contains(normalized)) {
      continue;
    }
    output << name << ": " << trim(value) << "\r\n";
  }
  output << "Connection: close\r\n\r\n";
  return output.str();
}

std::string serialize_client_response(const HttpResponse& response) {
  const auto tokens = connection_tokens(response.headers);
  std::ostringstream output;
  output << "HTTP/1.1 " << response.status_code << ' ' << response.reason << "\r\n"
         << "Content-Length: " << response.body.size() << "\r\n";
  for (const auto& [name, value] : response.headers) {
    const auto normalized = lower(name);
    if (normalized == "content-length" || standard_hop_header(name) ||
        tokens.contains(normalized)) {
      continue;
    }
    output << name << ": " << trim(value) << "\r\n";
  }
  output << "Connection: close\r\n\r\n" << response.body;
  return output.str();
}

std::string make_error_response(int status_code, std::string_view reason,
                                std::string_view detail) {
  std::string body(detail);
  body.push_back('\n');
  std::ostringstream output;
  output << "HTTP/1.1 " << status_code << ' ' << reason << "\r\n"
         << "Content-Type: text/plain; charset=utf-8\r\n"
         << "Content-Length: " << body.size() << "\r\n"
         << "Connection: close\r\n\r\n" << body;
  return output.str();
}

}  // namespace surge
