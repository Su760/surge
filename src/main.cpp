#include "surge/config.hpp"
#include "surge/reactor.hpp"

#include <exception>
#include <iostream>

int main(int argc, char** argv) {
  const auto parsed = surge::parse_config(argc, argv);
  if (parsed.show_help) {
    std::cout << surge::usage();
    return 0;
  }
  if (!parsed.error.empty()) {
    std::cerr << "surge: " << parsed.error << '\n' << surge::usage();
    return 2;
  }
  try {
    surge::Reactor reactor(parsed.config);
    return reactor.run();
  } catch (const std::exception& error) {
    std::cerr << "surge: fatal: " << error.what() << '\n';
    return 1;
  }
}
