# Task #49 compiles the proven Tasks #44-#48 main.c inside the USB ownership
# coordinator translation unit. Task #87 wraps esp_tinyusb's strong mount and
# SCSI START STOP callbacks: the mount seam gates SetConfiguration on proven
# Device-FS release, while START STOP distinguishes explicit host eject from
# generic suspend/detach.
idf_component_register(SRCS "task49_runtime.c" "task87_wake_recovery.c"
                       INCLUDE_DIRS "."
                       REQUIRES recorder spi_flash)

idf_build_set_property(LINK_OPTIONS
                       "-Wl,--wrap=tud_mount_cb"
                       "-Wl,--wrap=tud_msc_start_stop_cb"
                       APPEND)
