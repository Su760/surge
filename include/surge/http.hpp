#pragma once

#include <cstddef>
#include <memory>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace surge {

enum class ParseState { in_progress, complete, error };

struct HttpRequest {
  std::string target;
  std::vector<std::pair<std::string, std::string>> headers;
};

struct HttpResponse {
  int status_code{0};
  std::string reason;
  std::vector<std::pair<std::string, std::string>> headers;
  std::string body;
};

class RequestParser {
 public:
  explicit RequestParser(std::size_t max_bytes);
  ~RequestParser();
  RequestParser(RequestParser&&) noexcept;
  RequestParser& operator=(RequestParser&&) noexcept;
  RequestParser(const RequestParser&) = delete;
  RequestParser& operator=(const RequestParser&) = delete;

  ParseState feed(std::string_view bytes);
  [[nodiscard]] const HttpRequest& request() const;
  [[nodiscard]] const std::string& error() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

class ResponseParser {
 public:
  explicit ResponseParser(std::size_t max_bytes);
  ~ResponseParser();
  ResponseParser(ResponseParser&&) noexcept;
  ResponseParser& operator=(ResponseParser&&) noexcept;
  ResponseParser(const ResponseParser&) = delete;
  ResponseParser& operator=(const ResponseParser&) = delete;

  ParseState feed(std::string_view bytes);
  ParseState finish();
  [[nodiscard]] const HttpResponse& response() const;
  [[nodiscard]] const std::string& error() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

std::string serialize_upstream_request(const HttpRequest& request,
                                       std::string_view upstream_authority);
std::string serialize_client_response(const HttpResponse& response);
std::string make_error_response(int status_code, std::string_view reason,
                                std::string_view detail);

}  // namespace surge
