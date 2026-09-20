#pragma once

#include "surge/config.hpp"

namespace surge {

class Reactor {
 public:
  explicit Reactor(Config config);
  ~Reactor();
  Reactor(const Reactor&) = delete;
  Reactor& operator=(const Reactor&) = delete;
  int run();

 private:
  struct Impl;
  Impl* impl_;
};

}  // namespace surge
