#pragma once

#include <cstddef>
#include <deque>
#include <mutex>
#include <utility>
#include <vector>

namespace surge {

template <typename T>
class BoundedQueue {
 public:
  explicit BoundedQueue(std::size_t capacity) : capacity_(capacity) {}

  BoundedQueue(const BoundedQueue&) = delete;
  BoundedQueue& operator=(const BoundedQueue&) = delete;

  bool try_push(T&& value) {
    std::lock_guard lock(mutex_);
    if (!accepting_ || values_.size() >= capacity_) return false;
    values_.push_back(std::move(value));
    return true;
  }

  std::vector<T> take_all() {
    std::lock_guard lock(mutex_);
    std::vector<T> result;
    result.reserve(values_.size());
    while (!values_.empty()) {
      result.push_back(std::move(values_.front()));
      values_.pop_front();
    }
    return result;
  }

  void close() {
    std::lock_guard lock(mutex_);
    accepting_ = false;
  }

  [[nodiscard]] std::size_t size() const {
    std::lock_guard lock(mutex_);
    return values_.size();
  }

 private:
  const std::size_t capacity_;
  mutable std::mutex mutex_;
  std::deque<T> values_;
  bool accepting_{true};
};

}  // namespace surge
