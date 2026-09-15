# M5Daylog recorder component (Tasks #44/#45/#46)

Task #44 scope: PDM mic → I2S/DMA → 32KB × 2 double buffer → microSD
`.wav.part` continuous PCM write. 16kHz / signed 16bit / mono PCM.

Task #45 scope (IM-008): WAV rotation / finalize — 30-minute +
midnight rotation with header-finalize, flush/close, and idempotent
`.wav.part` → `.wav` rename (USB / low-battery / safe-stop share the
same idempotent finalize; no double close, no double rename).

Task #46 scope (IM-009): boot power-loss recovery / quarantine —
residual `.wav.part` scan, WAV header rebuild from the written PCM
payload length with tail-only sample truncation, recovered `.wav`
finalize, unrecoverable files moved to `quarantine/` with rename() only
(never auto-deleted), results appended to `events.jsonl` as counts.

Parent Map: #1. Tasks: #44 (`IM-007`), #45 (`IM-008`), #46 (`IM-009`).
Related Spec: #36 (`S-003`).

## Files

```text
components/recorder/
  CMakeLists.txt
  README.md                      # this file
  include/recorder_config.h      # sample rate / format / buffer + rotation + recovery paths
  include/pcm_pipeline.h         # portable ping-pong double buffer + counters
  pcm_pipeline.c                 # portable, no ESP-IDF dependency
  include/wav_part.h             # portable RIFF/WAVE `.part` framing over stdio
  wav_part.c                     # portable, no ESP-IDF dependency
  include/wav_rotation.h         # portable rotation/finalize over stdio (#45)
  wav_rotation.c                 # portable, no ESP-IDF dependency (#45)
  include/wav_recovery.h         # portable boot recovery/quarantine over stdio (#46)
  wav_recovery.c                 # portable, no ESP-IDF dependency (#46)
  include/i2s_pdm_capture.h      # ESP-IDF PDM RX wrapper (pins from caller)
  i2s_pdm_capture.c              # ESP-IDF only (`ESP_PLATFORM`)
  include/sd_pcm_sink.h          # `.wav.part` file sink + write-latency stats
  sd_pcm_sink.c                  # stdio + `esp_timer` latency when available
  include/sd_mount.h             # microSD SPI mount + recording/date/quarantine-dirs API
  sd_mount.c                     # ESP-IDF only (`ESP_PLATFORM`)
```

## Board bring-up (`sd_mount.c`, `recorder_config.h`)

- PDM mic: CLK GPIO40 / DAT GPIO41 (M5Capsule v1.1 baseline, overridable
  via `-DRECORDER_PDM_CLK_PIN=...` / `-DRECORDER_PDM_DATA_PIN=...`).
- microSD: SPI bus CS 11 / MOSI 12 / CLK 14 / MISO 39, mounted at
  `/sdcard` via `sd_mount_recordings()`. The card is never formatted on
  mount failure — failures surface as ERROR instead.
- Live-recording directories plus Task #45 date directories plus Task #46
  quarantine are created: `/sdcard/M5DAYLOG`, `/sdcard/M5DAYLOG/recordings`,
  `recordings/YYYY-MM-DD/` via `sd_mount_ensure_date_dir()` for midnight
  rotation, and `/sdcard/M5DAYLOG/quarantine/` via
  `sd_mount_ensure_quarantine_dir()` for power-loss isolation (never
  auto-deleted). Later retention/manifest state is out of scope (#47+).
- Runtime recording paths have the Spec #36 shape
  (`recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav.part`); the
  recording-id field is a per-boot segment counter placeholder until later
  Tasks own full identity. The committed default path is a build-time
  fallback only.
- Every bring-up step is fail-loud: mount / mic-init / `.part`-open
  failures enter ERROR, never a silent "recording" state.

## Contract

- Audio format is fixed: 16kHz / signed 16bit little-endian / mono PCM.
  See `recorder_config.h`. No resampling or multi-channel handling here.
- Double buffer is fixed at `32KB × 2` (`RECORDER_BUFFER_BYTES ×
  RECORDER_BUFFER_SLOTS`). The producer (I2S read) never blocks the DMA
  path: when both slots are full the incoming bytes are **dropped and
  counted as software loss** (`buffer_overflow`, `buffer_drop_bytes`,
  `buffer_drop_samples`), never silently overwritten and never mixed into
  driver `dma_*` counters. Silent failure is forbidden (Spec #36).
- `.part` files carry a standard 44-byte RIFF/WAVE PCM header written at
  open with zeroed sizes, followed by the raw PCM payload. `wav_part_flush`
  / `wav_part_close` seek back and patch `ChunkSize` / `Subchunk2Size` from
  the accumulated payload length, so a `.part` is decodeable by a standard
  decoder after any flush. Odd trailing bytes are truncated to the sample
  boundary (2 bytes); only the truncated tail is discarded.
- Rotation / finalize `.wav.part → .wav` is Task #45 scope
  (`wav_rotation.h/.c` + writer wiring in `main.c`): header finalize,
  flush/close, then rename to `.wav` on 30-minute elapsed, midnight date
  change, USB connection, low battery, or safe-stop request. Close is
  idempotent across simultaneous events (first call does I/O; duplicates
  return the cached result with no double close and no double rename).
  This component never deletes audio (only `rename()`, never `remove()`).
  `sd_pcm_sink` appends to one caller-provided `.wav.part` path (suffix
  enforced at open, never a bare `.part`).
- Power-loss recovery / quarantine is Task #46 scope
  (`wav_recovery.h/.c` + RECOVER step in `main.c` writer bring-up after
  mount, before the new segment opens): residual `.wav.part` files under
  `recordings/` (top level plus one-level date subdirectories) are
  recovered by rebuilding the 44-byte header from `file_size - 44` with
  the fixed 16kHz/16bit/mono format; an odd trailing byte is truncated to
  the sample boundary only. Files too small for a header, with an invalid
  RIFF/WAVE/PCM header or format mismatch, or colliding with an existing
  `.wav` are moved to `quarantine/` with `rename()` (numeric uniqueness on
  collision) and never auto-deleted. A summary plus per-file JSON lines
  (basename + `pcm_bytes` + result, sanitized to filename-safe chars) are
  appended to `events.jsonl`; fatal scan/dir failures enter ERROR, never
  silent recording. Later retention/manifest bookkeeping stays out of
  scope (#47 and later).
- microSD mount and recording directories are Task #44 bring-up
  (`sd_mount.c`: `/sdcard` + `/sdcard/M5DAYLOG/recordings` + Task #46
  `/sdcard/M5DAYLOG/quarantine`). The sink
  writes to the mounted path; open/write failures are fail-loud (`false` +
  counter increment + caller logs ERROR); the caller must not report a
  recording state while the sink is failed.
- PDM pins arrive via `pdm_capture_config_t`, defaulting to the Task #44
  M5Capsule v1.1 baseline in `recorder_config.h` (CLK 40 / DAT 41,
  overridable with `-D` flags). Unset pins (`< 0`) fail init with
  `ESP_ERR_INVALID_ARG`.
- Overrun/drop semantics: proven driver DMA loss arrives ONLY from the
  ESP-IDF I2S RX queue-overflow callback (`i2s_event_data_t.size` per
  event, `i2s_channel_register_event_callback(handle, &cbs, NULL)`,
  `IRAM_ATTR`, ISR-safe armed flag + counters under one spinlock,
  deterministically reset on init). Every callback is preserved and
  accumulated exactly (`dma_overrun_events`, `dma_drop_bytes`,
  `dma_drop_samples`) via a drain that runs on EVERY read — including
  timeouts, zero-byte reads, STOP, and fatal reads — plus a quiescent
  stop-then-final-drain (`pdm_capture_stop_and_drain_final` disables the
  RX channel before the final drain so no callback can fire afterwards;
  a failed disable keeps `enabled`, zeroes the snapshot, and stays
  fail-loud/retryable instead of claiming quiescence);
  never collapsed into a single flag, never inferred from a short read.
  A stall (`dma_read_stalls`) is a timeout OR ESP_OK short read with NO
  driver overflow on that same read (!snap_pending); overflow and stall
  are never double-classified. Diagnostics report `dma_*` separately from
  software `buffer_*` alongside `buffer_overflow`, `sd_err`, and worst SD
  latency.
- PDM slot: the M5Capsule microphone is the RIGHT slot
  (`I2S_PDM_SLOT_RIGHT`; M5Unified `input_only_right`), never the
  mono-default LEFT. Output stays 16kHz / signed 16bit / mono PCM.
- Logging carries only stage / count / status-class metadata. No audio
  bytes, transcripts, credentials, or secrets are logged (see `SECURITY.md`).

## Data flow (`main.c`: capture task + writer task, event-group lifecycle)

```text
recorder_capture_task (prio 5): wait WRITER_READY handshake → re-check STOP
    (never start mic on STOP) → PDM mic → I2S DMA → pdm_capture_read →
    always drain driver overflow snapshot (exact events/bytes, even on
    timeout/zero/STOP/fatal) → re-check STOP (never enqueue past a dead
    sink) → [lock] exact stall note (timeout/short && !overflow) +
    driver-overflow note + pcm_pipeline_produce (slot A/B) → set SLOT_FULL
    → quiescent stop-and-final-drain
    (pdm_capture_stop_and_drain_final; failed disable stays retryable)
    → fail-closed deinit (checked fail-loud, never destroys after failure)
recorder_writer_task (prio 4): mount + quarantine ensure + RECOVER scan
    (residual `.wav.part` → recovered `.wav` / `quarantine/`, counts to
    `events.jsonl` + `stage: recover` log; fatal scan enters ERROR) +
    date dir + segment open →
    set WRITER_READY → on SLOT_FULL: [lock] peek full slot → [unlock] →
    sd_pcm_sink_write_chunk (slow, lock released: capture fills the other
    slot meanwhile) → [lock] release + latency note → every 4 slots:
    wav_part_flush (header patch) + running diagnostics → periodic
    rotation poll (30min/size, midnight date change): WITHOUT lock and
    WITHOUT stopping capture → wav_rotation_finalize_once (close+rename,
    idempotent) → ensure new date dir → open new segment → STOP:
    drain-then-exit, idempotent finalize (USB/low-battery/safe-stop share
    one close+rename, no double close/rename), unmount
```

One app-lifetime event group is the only cross-task channel (SLOT_FULL,
STOP, WRITER_READY); no task handle is ever published, so no stale-handle
signal is possible, and creation order decides nothing under SMP.

`app_main` starts the writer (owns mount + sink) then the capture task
after scaffold boot logs. Any bring-up or I/O failure logs
`stage: record, result: error` and tears both tasks down into a
non-recording ERROR state — it never idles as if recording (Spec #36
silent-state prohibition). When both slots are full, payload is dropped
and counted, never overwritten.
Wi-Fi / BLE / RGB LED handling stays at the scaffold default (not enabled
here); radio/LED policy is owned by Spec #36 and later Tasks.

## Host tests

Portable logic (`pcm_pipeline.c`, `wav_part.c`, `wav_rotation.c`,
`wav_recovery.c`, `recorder_config.h`) has no ESP-IDF dependency. Host
contract tests live in `firmware/tests/` and run with stdlib-only pytest
(no ESP-IDF, no device, no network):

```sh
python3 -m pytest firmware/tests -v
```

`test_wav_part.py` checks the 44-byte header vectors, odd-tail truncation,
and `wave`-module decodeability of synthetic payloads. `test_pcm_pipeline.py`
locks the ping-pong / overflow-counting rules and the 32KB × 2 constants.
`test_wav_rotation.py` locks Task #45 rotation/finalize: 30min/midnight
decision, date-dir path building, idempotent close+rename (no double
close/rename), finalized-`wave` decodeability, and the writer stack budget
(static path storage, sized `RECORDER_WRITER_STACK_BYTES`, high-water log).
`test_recording_contract.py`
guards the 16kHz/16bit/mono format, `.wav.part` suffix discipline,
fail-loud symbols, producer/consumer structure, overrun/drop accounting,
and later-task scope boundaries (retention/manifest stay out; #45
rotation and #46 recovery/quarantine are in scope).
`test_wav_recovery.py` locks Task #46 recovery/quarantine: header
rebuild from payload length, odd-tail-only truncation, quarantine
isolation without deletion, collision handling (never overwrite),
date-dir scan, `events.jsonl` results, boot wiring (`stage: recover`,
quarantine ensure, fail-loud scan), and `wave`-module decodeability of
recovered files with synthetic payloads.

## Device evidence (recorded in the Task PR, never in-repo)

- 1-hour run counters: `samples_captured`, driver `dma_drop_* = 0` with
  `dma_overrun_events = 0`, software `buffer_overflow = 0` with
  `buffer_drop_* = 0`, `sd_write_errors = 0`, max SD latency.
- Generated `.wav.part` (+ flushed header) opened with a standard decoder.
- Task #45 boundary evidence: rotation-boundary waveform/duration/decode
  results plus `stage: rotate` / `stage: finalize` event logs showing
  finalized `.wav` corruption 0, unintended gap <=100ms, midnight date-dir
  switch, and duplicate-stop idempotency (no double close/rename).
- Task #46 recovery evidence (synthetic power-cut runs, never real audio):
  residual `.wav.part` sizes, `stage: recover` counts
  (`scanned/recovered/quarantined/errors`), `events.jsonl` recovery lines,
  `wave`-module decodeability of every recovered `.wav` (corruption 0),
  and quarantine isolation of the unrecoverable cases with no auto-delete.
- Writer stack high-water (`stage: record, result: stack, writer_hw: ...`)
  from a normal run, from each successful rotation while recording
  continues, and from the boot-without-SD fail-loud path; no stack
  overflow or reboot loop on SD mount failure.
- `idf.py build` / flash / boot log tails, ESP-IDF pin `v5.5.5`.
- Observed boot/capture log lines; any `-D` pin overrides used for the run
  (baseline pins are committed in `recorder_config.h`).

No real audio, transcripts, or device captures are committed to the repo.

## Hardware-readiness note

This implementation targets the ESP-IDF v5.5.5 API but has **not** been
compiled with the IDF toolchain or executed on hardware in this session
(no toolchain/host execution available here). It must **not** be treated
as hardware-ready: a real `idf.py build` on the next exact published head
must succeed before any flash, followed by flash/boot logs, 1-hour run
counters, and standard-decoder verification of the `.wav.part` output.
Any API/compile incompatibility found at build time is a blocking defect
for this Task.
