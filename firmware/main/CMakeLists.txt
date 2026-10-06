# Task #49 compiles the proven Tasks #44-#48 main.c inside the USB ownership
# coordinator translation unit. Task #87 uses esp_tinyusb's device ATTACHED
# event only as a provisional connection trigger: it soft-disconnects, completes
# the durable APP -> USB storage handoff, then reconnects with an already
# USB-owned MSC LUN. TinyUSB's SCSI-completion callback observes explicit eject;
# the START STOP wrapper remains a compatibility seam for toolchain layouts
# where that callback is externally referenced.
idf_component_register(SRCS "task49_runtime.c" "task87_wake_recovery.c"
                       INCLUDE_DIRS "."
                       REQUIRES recorder spi_flash)

idf_build_set_property(LINK_OPTIONS
                       "-Wl,--wrap=tud_msc_start_stop_cb"
                       APPEND)
