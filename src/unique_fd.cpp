#include "surge/unique_fd.hpp"

#include <unistd.h>

#include <utility>

namespace surge {

UniqueFd::UniqueFd(int fd) noexcept : fd_(fd) {}

UniqueFd::~UniqueFd() { reset(); }

UniqueFd::UniqueFd(UniqueFd&& other) noexcept : fd_(other.release()) {}

UniqueFd& UniqueFd::operator=(UniqueFd&& other) noexcept {
  if (this != &other) reset(other.release());
  return *this;
}

int UniqueFd::get() const noexcept { return fd_; }

UniqueFd::operator bool() const noexcept { return fd_ >= 0; }

int UniqueFd::release() noexcept { return std::exchange(fd_, -1); }

void UniqueFd::reset(int fd) noexcept {
  if (fd_ == fd) return;
  if (fd_ >= 0) ::close(fd_);
  fd_ = fd;
}

}  // namespace surge
