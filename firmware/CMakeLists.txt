cmake_minimum_required(VERSION 3.16)

# M5Daylog device firmware (ESP-IDF native).
# ESP-IDF version pin: v5.5.5 (see firmware/README.md).
# Arduino runtime / PlatformIO are intentionally not used (Decision #11).

# Protocol v1 valid command envelopes need at most two container levels
# (root + args). Keep limited headroom for rejecting invalid arguments while
# bounding cJSON's recursive parser stack before command dispatch.
set(M5DAYLOG_CJSON_NESTING_LIMIT 4)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(m5daylog)

# ESP-IDF v5.5.x ships cJSON with its upstream default nesting limit (1000).
# Apply the protocol-specific bound to the actual production json component.
idf_component_get_property(m5daylog_json_lib json COMPONENT_LIB)
target_compile_definitions(
    ${m5daylog_json_lib}
    PRIVATE CJSON_NESTING_LIMIT=${M5DAYLOG_CJSON_NESTING_LIMIT}
)
