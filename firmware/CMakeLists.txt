cmake_minimum_required(VERSION 3.16)

# M5Daylog device firmware (ESP-IDF native).
# ESP-IDF version pin: v5.5.5 (see firmware/README.md).
# Arduino runtime / PlatformIO are intentionally not used (Decision #11).

# CDC v1 valid envelopes require root + args containers. Keep bounded
# validation headroom while preventing untrusted USB JSON from consuming the
# fixed CDC worker stack through cJSON recursion.
set(M5DAYLOG_CJSON_NESTING_LIMIT 4)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(m5daylog)

idf_component_get_property(m5daylog_json_lib json COMPONENT_LIB)
idf_component_get_property(m5daylog_recorder_lib recorder COMPONENT_LIB)
target_compile_definitions(
    ${m5daylog_json_lib}
    PRIVATE CJSON_NESTING_LIMIT=${M5DAYLOG_CJSON_NESTING_LIMIT}
)

# Build-only stack-usage sidecars feed the exact-target CI budget check.
target_compile_options(${m5daylog_json_lib} PRIVATE -fstack-usage)
target_compile_options(${m5daylog_recorder_lib} PRIVATE -fstack-usage)
