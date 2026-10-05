cmake_minimum_required(VERSION 3.21)

project(GDLayoutStripper VERSION 0.1.0)

enable_language(RC)

add_library(${PROJECT_NAME} SHARED
    src/deco_toggle.cpp
    src/highlight_system.cpp
    src/main.cpp
    src/prior_style.cpp
)

target_compile_features(${PROJECT_NAME} PUBLIC cxx_std_23)
set_target_properties(${PROJECT_NAME} PROPERTIES PREFIX "")

find_package(Python3 REQUIRED COMPONENTS Interpreter)
file(GLOB_RECURSE GD_CLASS_SRC CONFIGURE_DEPENDS
    ${CMAKE_CURRENT_SOURCE_DIR}/../bridge/*.py
    ${CMAKE_CURRENT_SOURCE_DIR}/../ml/*.py
)
set(GD_SEM_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../gd/semantics/2.2081)
set(GD_GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated)
set(GD_GEN_SEM ${GD_GEN_DIR}/native_sem.hpp)
add_custom_command(
    OUTPUT ${GD_GEN_SEM}
    COMMAND ${CMAKE_COMMAND} -E make_directory ${GD_GEN_DIR}
    COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/gen_native.py
        ${GD_SEM_DIR}/catalogue.json
        ${GD_SEM_DIR}/effects.json
        ${GD_GEN_SEM}
    DEPENDS
        ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/gen_native.py
        ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/compile_layout.py
        ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/run_decimal.py
        ${CMAKE_CURRENT_SOURCE_DIR}/../scripts/run_highlight.py
        ${GD_SEM_DIR}/catalogue.json
        ${GD_SEM_DIR}/effects.json
        ${GD_CLASS_SRC}
    VERBATIM
)
add_executable(GDStripWorker
    src/native_worker.cpp
    ${GD_GEN_SEM}
)
target_compile_features(GDStripWorker PUBLIC cxx_std_23)
target_include_directories(GDStripWorker PRIVATE ${GD_GEN_DIR})
target_link_libraries(GDStripWorker PRIVATE bcrypt)
set_property(
    TARGET GDStripWorker PROPERTY MSVC_RUNTIME_LIBRARY
    "MultiThreaded$<$<CONFIG:Debug>:Debug>"
)
set_target_properties(GDStripWorker PROPERTIES OUTPUT_NAME gd-layout-worker)
set(GD_WORK_EXE ${CMAKE_CURRENT_BINARY_DIR}/gd-layout-worker.exe)
add_custom_command(TARGET GDStripWorker POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:GDStripWorker> ${GD_WORK_EXE}
)
configure_file(src/worker.rc.in ${GD_GEN_DIR}/worker.rc @ONLY)
add_dependencies(${PROJECT_NAME} GDStripWorker)
set_property(
    SOURCE ${GD_GEN_DIR}/worker.rc PROPERTY OBJECT_DEPENDS ${GD_WORK_EXE}
)
target_sources(${PROJECT_NAME} PRIVATE ${GD_GEN_SEM} ${GD_GEN_DIR}/worker.rc)
target_include_directories(${PROJECT_NAME} PRIVATE ${GD_GEN_DIR})
add_custom_target(source_guard ALL
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/../tests/are_comments.py
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..
)
add_dependencies(${PROJECT_NAME} source_guard)

if(NOT DEFINED ENV{GEODE_SDK})
    message(FATAL_ERROR "GEODE_SDK is required")
endif()

add_subdirectory($ENV{GEODE_SDK} ${CMAKE_CURRENT_BINARY_DIR}/geode)
setup_geode_mod(${PROJECT_NAME} DONT_INSTALL)
set(GD_FMT_DIR ${GD_GEN_DIR}/fmt_include)
file(COPY ${FMT_SOURCE_DIR}/include/fmt DESTINATION ${GD_FMT_DIR})
foreach(GD_FMT_TARGET ${PROJECT_NAME} GeodeBindings arc asp)
    if(TARGET ${GD_FMT_TARGET})
        target_include_directories(${GD_FMT_TARGET} BEFORE PRIVATE ${GD_FMT_DIR})
    endif()
endforeach()
