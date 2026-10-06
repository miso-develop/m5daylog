# Task #49 compiles the proven Tasks #44-#48 main.c inside the USB ownership
# coordinator translation unit. Task #87 uses esp_tinyusb's first ATTACHED only
# as provisional configuration evidence. A completed MSC SCSI command proves
# class binding, then the recorder coordinator disconnects, completes the
# durable APP -> USB storage handoff, and reconnects with an already USB-owned
# MSC LUN. Explicit eject is observed only from TinyUSB's post-status
# tud_msc_scsi_complete_cb; no linker callback wrapper is used.
idf_component_register(SRCS "task49_runtime.c" "task87_wake_recovery.c"
                       INCLUDE_DIRS "."
                       REQUIRES recorder spi_flash)
