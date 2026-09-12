# M5Daylog firmware scaffold (Task #42)

Target: **M5Capsule v1.1** (`ESP32-S3FN8`, 8MB flash).
Toolchain: **ESP-IDF native v5.5.5**. Arduino runtime / Arduino as Component /
PlatformIO are not used (Decision #11).

Parent Map: #1. Depends on: #11, #34. Related Spec: #36.

## Layout

```text
firmware/
  CMakeLists.txt        # project entrypoint, includes $IDF_PATH project.cmake
  sdkconfig.defaults    # CONFIG_IDF_TARGET=esp32s3 + log default
  main/
    CMakeLists.txt      # idf_component_register for main.c
    main.c              # scaffold boot + serial output + idle loop
  components/
    README.md           # placeholder; later Tasks add minimal HW abstraction here
```

`sdkconfig` (generated), `build/`, and `managed_components/` stay local-only
and are already ignored by the repository `.gitignore`.

## ESP-IDF v5.5.5 setup

```sh
git clone -b v5.5.5 --recursive https://github.com/espressif/esp-idf.git esp-idf-v5.5.5
cd esp-idf-v5.5.5
./install.sh esp32s3
. ./export.sh
```

Verify:

```sh
idf.py --version
```

Expected pin: `v5.5.5`.

## Build / flash / monitor (M5Capsule v1.1 over USB-C serial)

Run from `firmware/`:

```sh
idf.py set-target esp32s3
idf.py build
idf.py -p <SERIAL_PORT> flash
idf.py -p <SERIAL_PORT> monitor
```

`<SERIAL_PORT>` is machine-local (for example `COMx` on Windows or
`/dev/ttyACM0` on Linux) and is intentionally not recorded here.

## Expected serial output (synthetic shape, not a device capture)

```text
I (xxx) m5daylog: m5daylog firmware scaffold boot
I (xxx) m5daylog: chip cores: 2, revision: x
I (xxx) m5daylog: flash size: 8 MB
I (xxx) m5daylog: idf version: v5.5.5
I (xxx) m5daylog: stage: scaffold, result: boot ok
I (xxx) m5daylog: stage: scaffold, result: idle
```

Only stage / count / status-class metadata is logged. No credential, audio
payload, transcript, or speaker data is emitted.

## M5Unified note

M5Unified is used only where M5Capsule hardware abstraction requires it, in
the minimal range (Decision #11). The scaffold does not add the M5Unified
dependency yet; a later recording/USB Task introduces it as a managed
component only if ESP-IDF drivers alone are insufficient.

## Scope

In scope for #42: scaffold files above, clean `idf.py build`, device boot,
serial output observable.

Task #44 (IM-007) adds `components/recorder/` (PDM→DMA→SD PCM capture:
16kHz/16bit/mono, 32KB × 2 ping-pong buffer with overrun/drop counters,
M5Capsule v1.1 bring-up with PDM CLK40/DAT41 and SD SPI mount at `/sdcard`,
`.wav.part` continuous write with flush-patched headers, fail-loud mic/SD
errors) wired into `main/` as capture + writer FreeRTOS tasks, plus
stdlib-only host contract tests in `firmware/tests/`
(`python3 -m pytest firmware/tests -v`). Rotation/finalize (#45),
recovery (#46), manifest (#47), and state machine (#48) remain out
of scope.

Out of scope: WAV rotation (#45), recovery (#46),
manifest (#47), state machine (#48), USB MSC/CDC (#49/#50), `pc/`,
`contracts/`, `fixtures/`, `.github/`, and security-rule changes.

## Evidence

Record in the Task PR: ESP-IDF version output, `idf.py build` log tail, flash
log tail, and observed boot lines. Use synthetic shape above as format
reference; never paste private recording content.
