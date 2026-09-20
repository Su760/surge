cmake_minimum_required(VERSION 3.25)
project(surge VERSION 0.1.0 LANGUAGES C CXX)

include(CTest)
include(FetchContent)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(SURGE_SANITIZER "" CACHE STRING "Enable address or undefined sanitizer")
set_property(CACHE SURGE_SANITIZER PROPERTY STRINGS "" address undefined)

if(SURGE_SANITIZER)
  if(NOT SURGE_SANITIZER MATCHES "^(address|undefined)$")
    message(FATAL_ERROR "SURGE_SANITIZER must be address or undefined")
  endif()
  add_compile_options(-fno-omit-frame-pointer -fsanitize=${SURGE_SANITIZER})
  add_link_options(-fno-omit-frame-pointer -fsanitize=${SURGE_SANITIZER})
endif()

set(LLHTTP_BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(LLHTTP_BUILD_STATIC_LIBS ON CACHE BOOL "" FORCE)
FetchContent_Declare(
  llhttp
  GIT_REPOSITORY https://github.com/nodejs/llhttp.git
  GIT_TAG 0e815792b167a9bd8ace259b95b7da953776c288
  GIT_SHALLOW FALSE
)
FetchContent_MakeAvailable(llhttp)

add_library(surge_core
  src/config.cpp
  src/http.cpp
  src/reactor.cpp
  src/unique_fd.cpp
)
target_include_directories(surge_core PUBLIC include)
target_link_libraries(surge_core PUBLIC llhttp::llhttp)
target_compile_options(surge_core PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)

add_executable(surge src/main.cpp)
target_link_libraries(surge PRIVATE surge_core)

if(BUILD_TESTING)
  add_executable(surge_unit_tests tests/unit_tests.cpp)
  target_link_libraries(surge_unit_tests PRIVATE surge_core)
  if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(surge_unit_tests PRIVATE -Wl,--wrap=close)
  endif()
  add_test(NAME unit COMMAND surge_unit_tests)

  find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter)
  add_test(
    NAME integration
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/integration_test.py
            --gateway $<TARGET_FILE:surge>
            --backend ${CMAKE_CURRENT_SOURCE_DIR}/tools/backend.py
  )
  set_tests_properties(integration PROPERTIES TIMEOUT 90)
endif()
