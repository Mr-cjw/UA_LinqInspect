cmake_minimum_required(VERSION 3.16)

project(qc_open62541_server VERSION 0.1.0 LANGUAGES C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

option(QC_WARNINGS_AS_ERRORS "Treat compiler warnings as errors" OFF)

# open62541 1.4.x package. The target is built with:
#   -DUA_ENABLE_ENCRYPTION=MBEDTLS
#   -DUA_ENABLE_METHODCALLS=ON
#   -DUA_ENABLE_SUBSCRIPTIONS_EVENTS=ON
# See hardware/README.md for the complete build recipe.
find_package(open62541 1.4 REQUIRED)

# Link mbedTLS explicitly. Static open62541 builds do not always propagate the
# private crypto dependency into the installed imported target, and this makes
# the Basic256Sha256 requirement visible in the application build.
find_package(PkgConfig QUIET)
set(QC_MBEDTLS_FOUND FALSE)
if(PkgConfig_FOUND)
    pkg_check_modules(MBEDTLS QUIET mbedtls mbedx509 mbedcrypto)
    if(MBEDTLS_FOUND)
        set(QC_MBEDTLS_FOUND TRUE)
        set(QC_MBEDTLS_INCLUDE_DIRS ${MBEDTLS_INCLUDE_DIRS})
        set(QC_MBEDTLS_LIBRARIES ${MBEDTLS_LIBRARIES})
    endif()
endif()

if(NOT QC_MBEDTLS_FOUND)
    find_path(MBEDTLS_INCLUDE_DIR NAMES mbedtls/ssl.h)
    find_library(MBEDTLS_LIBRARY NAMES mbedtls)
    find_library(MBEDX509_LIBRARY NAMES mbedx509)
    find_library(MBEDCRYPTO_LIBRARY NAMES mbedcrypto)
    if(NOT MBEDTLS_INCLUDE_DIR OR NOT MBEDTLS_LIBRARY OR
       NOT MBEDX509_LIBRARY OR NOT MBEDCRYPTO_LIBRARY)
        message(FATAL_ERROR
            "mbedTLS development files not found. Install libmbedtls-dev or set "
            "MBEDTLS_INCLUDE_DIR/MBEDTLS_LIBRARY/MBEDX509_LIBRARY/MBEDCRYPTO_LIBRARY.")
    endif()
    set(QC_MBEDTLS_FOUND TRUE)
    set(QC_MBEDTLS_INCLUDE_DIRS ${MBEDTLS_INCLUDE_DIR})
    set(QC_MBEDTLS_LIBRARIES
        ${MBEDTLS_LIBRARY}
        ${MBEDX509_LIBRARY}
        ${MBEDCRYPTO_LIBRARY}
    )
endif()

add_executable(qc_open62541_server
    main.c
    qc_model.c
    qc_model.h
)

target_link_libraries(qc_open62541_server PRIVATE
    open62541::open62541
    ${QC_MBEDTLS_LIBRARIES}
)

target_include_directories(qc_open62541_server PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    ${QC_MBEDTLS_INCLUDE_DIRS}
)

target_compile_options(qc_open62541_server PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -Wconversion
    -Wshadow
    -Wformat=2
)

if(QC_WARNINGS_AS_ERRORS)
    target_compile_options(qc_open62541_server PRIVATE -Werror)
endif()

install(TARGETS qc_open62541_server RUNTIME DESTINATION bin)
