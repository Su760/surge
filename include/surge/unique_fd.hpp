#pragma once

namespace surge {

class UniqueFd {
 public:
  UniqueFd() noexcept = default;
  explicit UniqueFd(int fd) noexcept;
  ~UniqueFd();

  UniqueFd(const UniqueFd&) = delete;
  UniqueFd& operator=(const UniqueFd&) = delete;
  UniqueFd(UniqueFd&& other) noexcept;
  UniqueFd& operator=(UniqueFd&& other) noexcept;

  [[nodiscard]] int get() const noexcept;
  [[nodiscard]] explicit operator bool() const noexcept;
  int release() noexcept;
  void reset(int fd = -1) noexcept;

 private:
  int fd_{-1};
};

}  // namespace surge
