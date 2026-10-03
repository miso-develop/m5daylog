# Task #49 compiles the proven Tasks #44-#48 main.c inside the USB ownership
# coordinator translation unit. main.c remains the source-contract surface;
# task49_runtime.c adds only lifecycle orchestration around it.
idf_component_register(SRCS "task49_runtime.c" INCLUDE_DIRS "." REQUIRES recorder spi_flash)
