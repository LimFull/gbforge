cmake_minimum_required(VERSION 3.20)
project(gbforge LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Analyzer: gb-recompiled's gbrecomp_core (MIT), pinned. Only the library is used —
# its SDL runtime and C emitter are not (see reports/gb-recompiled-spike.md).
# Use a local clone with -DFETCHCONTENT_SOURCE_DIR_GBRECOMPILED=/path/to/gb-recompiled
set(GBRECOMPILED_COMMIT 9150f87d82fa98abcb6ea22329170463f9702eb8)
set(CMAKE_DISABLE_FIND_PACKAGE_SDL2 ON)
include(FetchContent)
FetchContent_Declare(gbrecompiled
    GIT_REPOSITORY https://github.com/arcanite24/gb-recompiled.git
    GIT_TAG ${GBRECOMPILED_COMMIT})
FetchContent_MakeAvailable(gbrecompiled)

add_executable(gbforge-core tools/core/main.cpp tools/core/codegen.cpp)
target_link_libraries(gbforge-core PRIVATE gbrecomp_core)
target_compile_definitions(gbforge-core PRIVATE GBRECOMPILED_COMMIT="${GBRECOMPILED_COMMIT}")
set_target_properties(gbforge-core PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR})
