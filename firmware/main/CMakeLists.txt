# Task #49 compiles the proven Tasks #44-#48 main.c inside the USB ownership
# coordinator translation unit. Task #87 uses esp_tinyusb's synchronous device
# ATTACHED event to enter the pre-configuration storage barrier and TinyUSB's
# SCSI-completion callback to observe explicit eject. The original GNU ld wraps
# remain compatibility seams, but correctness does not depend on their reaching
# TinyUSB calls made inside the defining translation units.
idf_component_register(SRCS "task49_runtime.c" "task87_wake_recovery.c"
                       INCLUDE_DIRS "."
                       REQUIRES recorder spi_flash)

idf_build_set_property(LINK_OPTIONS
                       "-Wl,--wrap=tud_mount_cb"
                       "-Wl,--wrap=tud_msc_start_stop_cb"
                       APPEND)
