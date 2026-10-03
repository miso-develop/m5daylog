# Task #50 compiles the proven recorder main.c inside the USB coordinator
# translation unit, preserving Task #49 exclusive MSC ownership while adding
# CDC JSON and RTC-correction orchestration.
idf_component_register(SRCS "task50_runtime.c" INCLUDE_DIRS "." REQUIRES recorder spi_flash)
