cmake_minimum_required(VERSION 3.16)
project(fe_native_ops LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O2 -march=native -DNDEBUG")

add_library(fe_ops INTERFACE)
target_include_directories(fe_ops INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}/include")

# ── pybind11 modules ─────────────────────────────────────────
find_package(pybind11 REQUIRED)

# Output directory — setup.py sets CMAKE_LIBRARY_OUTPUT_DIRECTORY to
# the factorengine/ package dir so .so files are importable directly.
if(CMAKE_LIBRARY_OUTPUT_DIRECTORY)
  set(_OUT_DIR "${CMAKE_LIBRARY_OUTPUT_DIRECTORY}")
else()
  set(_OUT_DIR "${CMAKE_CURRENT_BINARY_DIR}")
endif()

# fe_ops: batch array-level kernels
pybind11_add_module(fe_ops_py pybind/fe_ops_bind.cpp)
set_target_properties(fe_ops_py PROPERTIES
  OUTPUT_NAME fe_ops
  LIBRARY_OUTPUT_DIRECTORY "${_OUT_DIR}")
target_link_libraries(fe_ops_py PRIVATE fe_ops)

# fe_runtime: DAG push-level runtime
pybind11_add_module(fe_runtime_py pybind/fe_runtime_bind.cpp)
set_target_properties(fe_runtime_py PROPERTIES
  OUTPUT_NAME fe_runtime
  LIBRARY_OUTPUT_DIRECTORY "${_OUT_DIR}")
target_link_libraries(fe_runtime_py PRIVATE fe_ops)
