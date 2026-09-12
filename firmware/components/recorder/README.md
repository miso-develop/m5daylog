# M5Daylog recorder component (Task #44)

Task #44 scope: PDM mic → I2S/DMA → 32KB × 2 double buffer → microSD
`.wav.part` continuous PCM write. 16kHz / signed 16bit / mono PCM.

Parent Map: #1. Task: #44 (`IM-007`). Related Spec: #36 (`S-003`).

## Files

```text
components/recorder/
  CMakeLists.txt
  README.md                      # this file
  include/recorder_config.h      # sample rate / format / buffer constants
  include/pcm_pipeline.h         # portable ping-pong double buffer + counters
  pcm_pipeline.c                 # portable, no ESP-IDF dependency
  include/wav_part.h             # portable RIFF/WAVE `.part` framing over stdio
  wav_part.c                     # portable, no ESP-IDF dependency
  include/i2s_pdm_capture.h      # ESP-IDF PDM RX wrapper (pins from caller)
  i2s_pdm_capture.c              # ESP-IDF only (`ESP_PLATFORM`)
  include/sd_pcm_sink.h          # `.part` file sink + write-latency stats
  sd_pcm_sink.c                  # stdio + `esp_timer` latency when available
```

## Board bring-up (`sd_mount.c`, `recorder_config.h`)

- PDM mic: CLK GPIO40 / DAT GPIO41 (M5Capsule v1.1 baseline, overridable
  via `-DRECORDER_PDM_CLK_PIN=...` / `-DRECORDER_PDM_DATA_PIN=...`).
- microSD: SPI bus CS 11 / MOSI 12 / CLK 14 / MISO 39, mounted at
  `/sdcard` via `sd_mount_recordings()`. The card is never formatted on
  mount failure — failures surface as ERROR instead.
- Only live-recording directories are created: `/sdcard/M5DAYLOG` and
  `/sdcard/M5DAYLOG/recordings`. Nothing else (no finalized-file,
  recovery, or manifest/retention state — later Tasks own those).
- Default recording path has the Spec #36 `.wav.part` shape
  (`HHMMSS_<recordingId>.wav.part`); runtime RTC/UUID naming arrives with
  later Tasks, so the committed default is a build-time fallback only.
- Every bring-up step is fail-loud: mount / mic-init / `.part`-open
  failures enter ERROR, never a silent "recording" state.

## Contract

- Audio format is fixed: 16kHz / signed 16bit little-endian / mono PCM.
  See `recorder_config.h`. No resampling or multi-channel handling here.
- Double buffer is fixed at `32KB × 2` (`RECORDER_BUFFER_BYTES ×
  RECORDER_BUFFER_SLOTS`). The producer (I2S read) never blocks the DMA
  path: when both slots are full the incoming bytes are **dropped and
  counted** (`buffer_overflow`, `dma_drop_bytes`, `dma_drop_samples`),
  never silently overwritten. Silent failure is forbidden (Spec #36).
- `.part` files carry a standard 44-byte RIFF/WAVE PCM header written at
  open with zeroed sizes, followed by the raw PCM payload. `wav_part_flush`
  / `wav_part_close` seek back and patch `ChunkSize` / `Subchunk2Size` from
  the accumulated payload length, so a `.part` is decodeable by a standard
  decoder after any flush. Odd trailing bytes are truncated to the sample
  boundary (2 bytes); only the truncated tail is discarded.
- Rotation / rename `.part → .wav` / recovery / manifest / retention are
  **out of scope** (Tasks #45/#46/#47). This component never deletes or
  renames files. `sd_pcm_sink` appends to one caller-provided `.part` path.
- SD mount is owned by board bring-up / later Tasks. The sink assumes the
  mount point already exists and the path is writable. Open/write failures
  are fail-loud (`false` + counter increment + caller logs ERROR); the
  caller must not report a recording state while the sink is failed.
- Board-specific PDM pins are **caller-provided** (`pdm_capture_config_t`).
  There are no hardcoded M5Capsule pin defaults in this component: pin
  bring-up is verified on hardware and recorded as Task #44 evidence, not
  guessed in source. Unset pins (`< 0`) fail init with
  `ESP_ERR_INVALID_ARG`.
- Logging carries only stage / count / status-class metadata. No audio
  bytes, transcripts, credentials, or secrets are logged (see `SECURITY.md`).

## Data flow (`main.c`)

```text
PDM mic → I2S DMA → pdm_capture_read → pcm_pipeline_produce (slot A/B)
     → while full slot → sd_pcm_sink_write_chunk → wav_part_write
     → periodic wav_part_flush (header patch so `.part` stays decodeable)
```

`app_main` starts a `recorder` FreeRTOS task after scaffold boot logs. If
mic init or `.part` open fails, the firmware logs
`stage: record, result: error` and stays in a non-recording ERROR state —
it never idles as if recording (Spec #36 silent-state prohibition).
Wi-Fi / BLE / RGB LED handling stays at the scaffold default (not enabled
here); radio/LED policy is owned by Spec #36 and later Tasks.

## Host tests

Portable logic (`pcm_pipeline.c`, `wav_part.c`, `recorder_config.h`) has no
ESP-IDF dependency. Host contract tests live in `firmware/tests/` and run
with stdlib-only pytest (no ESP-IDF, no device, no network):

```sh
python3 -m pytest firmware/tests -v
```

`test_wav_part.py` checks the 44-byte header vectors, odd-tail truncation,
and `wave`-module decodeability of synthetic payloads. `test_pcm_pipeline.py`
locks the ping-pong / overflow-counting rules and the 32KB × 2 constants.
`test_recording_contract.py` guards the 16kHz/16bit/mono format, `.part`
suffix discipline, fail-loud symbols, and #45/#46 scope boundaries (no
rename/recovery/manifest logic in this component).

## Device evidence (recorded in the Task #44 PR, never in-repo)

- 1-hour run counters: `samples_captured`, `dma_drop_* = 0`,
  `buffer_overflow = 0`, `sd_write_errors = 0`, max SD latency.
- Generated `.wav.part` (+ flushed header) opened with a standard decoder.
- `idf.py build` / flash / boot log tails, ESP-IDF pin `v5.5.5`.
- Real board pins used for the run (machine-local setup, not committed).

No real audio, transcripts, or device captures are committed to the repo.

## Hardware-readiness note

This implementation targets the ESP-IDF v5.5.5 API but has **not** been
compiled with the IDF toolchain or executed on hardware in this session
(no toolchain/host execution available here). It must **not** be treated
as hardware-ready until the privileged executor records: `idf.py build`
success, flash/boot logs, 1-hour run counters, and standard-decoder
verification of the `.wav.part` output. Any API/compile incompatibility
found at build time is a blocking defect for this Task.
