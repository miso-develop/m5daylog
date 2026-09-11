cmake_minimum_required(VERSION 3.16)

# M5Daylog device firmware (ESP-IDF native).
# ESP-IDF version pin: v5.5.5 (see firmware/README.md).
# Arduino runtime / PlatformIO are intentionally not used (Decision #11).

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(m5daylog)
