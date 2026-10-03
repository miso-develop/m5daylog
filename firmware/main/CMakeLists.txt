# Task #49 compiles the proven Tasks #44-#48 main.c inside the USB ownership
# coordinator translation unit. Task #87 wraps esp_tinyusb's strong SCSI
# START STOP callback so explicit host eject can be distinguished from generic
# suspend/detach without duplicating the callback symbol.
idf_component_register(SRCS "task49_runtime.c" INCLUDE_DIRS "." REQUIRES recorder spi_flash)

idf_build_set_property(LINK_OPTIONS "-Wl,--wrap=tud_msc_start_stop_cb" APPEND)
