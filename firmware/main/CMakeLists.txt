# Tasks #49/#87 compile the proven recorder main.c inside the USB ownership
# coordinator translation unit. D-031 adds the minimal canonical CDC v1
# RELEASE_STORAGE foundation to the same composite TinyUSB device. Acceptance
# closes MSC/ordinary-CDC admission before the success response; response
# completion then signals coordinator-owned teardown/quiescence.
idf_component_register(SRCS "task49_runtime.c" "task87_wake_recovery.c"
                       INCLUDE_DIRS "."
                       REQUIRES recorder spi_flash)
