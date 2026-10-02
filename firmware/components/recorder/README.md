# M5Daylog recorder component (Tasks #44/#45/#46/#47)

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

Task #47 scope (IM-010): device identity + manifest/integrity —
first-boot UUIDv4 `deviceId` (NVS `m5daylog/device_id`, stable across
boots), `device.json` generation (Spec #35 S-002, mismatch/unknown
schema never auto-overwritten), per-segment UUIDv4 `recordingId`,
incremental SHA-256 over the WAV file bytes entire (lowercase hex 64,
must match PC recomputation), finalized/recovered entries in
`manifest.json` via `manifest.tmp` full-write + flush + atomic rename
(power-loss preserves the old manifest; same recordingId with a
different hash reports CONFLICT and never overwrites).

Parent Map: #1. Tasks: #44 (`IM-007`), #45 (`IM-008`), #46 (`IM-009`).
Related Spec: #36 (`S-003`).

## Files

```text
components/recorder/
  CMakeLists.txt
  README.md                      # this file
  include/recorder_config.h      # sample rate / format / buffer + rotation + recovery + metadata paths
  include/pcm_pipeline.h         # portable ping-pong double buffer + counters
  pcm_pipeline.c                 # portable, no ESP-IDF dependency
  include/wav_part.h             # portable RIFF/WAVE `.part` framing over stdio
  wav_part.c                     # portable, no ESP-IDF dependency
  include/wav_rotation.h         # portable rotation/finalize over stdio (#45)
  wav_rotation.c                 # portable, no ESP-IDF dependency (#45)
  include/wav_recovery.h         # portable boot recovery/quarantine over stdio (#46)
  wav_recovery.c                 # portable, no ESP-IDF dependency (#46)
  include/sha256.h               # portable incremental SHA-256 over stdio (#47)
  sha256.c                       # portable, no ESP-IDF dependency (#47)
  include/device_identity.h      # portable UUID/device.json + ESP NVS ensure (#47)
  device_identity.c              # portable subset + ESP_PLATFORM NVS only (#47)
  include/device_manifest.h      # portable manifest/integrity over stdio (#47)
  device_manifest.c              # portable, no ESP-IDF dependency (#47)
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
  auto-deleted). Task #47 metadata files (`device.json`, `manifest.json`
  via `manifest.tmp`) live in the same root and are owned by
  `device_identity`/`device_manifest` + the writer task, not the mount
  module. Processed-ACK retention stays out of scope (later Task).
- Runtime recording paths have the Spec #36 shape
  (`recordings/YYYY-MM-DD/HHMMSS_<recordingId>.wav.part`); Task #47 fills
  the recording-id field with a fresh UUIDv4 per segment (122 hardware-RNG
  bits; the stable NVS `deviceId` is the device identity, the per-segment
  UUID is the cross-system primary key). The committed default path is a
  build-time fallback only.
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
   Audio is never deleted (only `rename()` moves `.wav`/`.wav.part`;
   the sole `remove()` uses in `device_manifest.c` are tmp/destination
   replace for the metadata atomic rename plus best-effort stale-tmp
   cleanup — never audio deletion, never auto-delete of unacknowledged
   recordings).
   `sd_pcm_sink` appends to one caller-provided `.wav.part` path (suffix
   enforced at open, never a bare `.part`).
- Device identity / manifest / integrity is Task #47 scope
  (`sha256.h/.c` + `device_identity.h/.c` + `device_manifest.h/.c` +
  writer wiring in `main.c`): NVS-stable UUIDv4 `deviceId`, `device.json`
  per Spec #35 (mismatch/unknown schema never auto-overwritten,
  fail-closed), fresh UUIDv4 `recordingId` per segment (the cross-system
  primary key), incremental SHA-256 over the WAV file bytes entire
  (lowercase hex, PC-recomputable), and `manifest.json` entries for every
  finalized/recovered `.wav` via `manifest.tmp` full-write + flush +
  atomic rename (crash preserves the old manifest; same recordingId with
  a different hash reports CONFLICT and never overwrites). Timestamps are
  UTC offset ISO-8601; only `.wav` (never `.wav.part`) is entered.
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
   silent recording. Task #47 syncs each recovered `.wav` into the
   manifest with state=recovered (unparseable names skipped, manifest I/O
   failure enters ERROR). Processed-ACK retention stays out of scope
   (later Task).
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
recorder_writer_task (prio 4): mount + NVS deviceId + device.json ensure
    (mismatch/unknown schema enters ERROR, never auto-overwrites) +
    manifest shell (promote valid manifest.tmp / create empty on first
    boot) + quarantine ensure + RECOVER scan (residual `.wav.part` →
    recovered `.wav` / `quarantine/`, counts to `events.jsonl` +
    `stage: recover` log; fatal scan enters ERROR) + recovered-`.wav`
    manifest sync (state=recovered, `stage: manifest` counts; manifest I/O
    failure enters ERROR) + date dir + segment open (fresh UUIDv4
    recordingId + UTC startedAt per segment) →
    set WRITER_READY → on SLOT_FULL: [lock] peek full slot → [unlock] →
    sd_pcm_sink_write_chunk (slow, lock released: capture fills the other
    slot meanwhile) → [lock] release + latency note → every 4 slots:
    wav_part_flush (header patch) + running diagnostics → periodic
    rotation poll (30min/size, midnight date change): WITHOUT lock and
    WITHOUT stopping capture → wav_rotation_finalize_once (close+rename,
    idempotent) → manifest record (SHA-256 file hash + tmp/rename upsert,
    state=finalized; failure stops fail-loud) → ensure new date dir →
    open new segment (fresh UUIDv4) → STOP: drain-then-exit, idempotent
    finalize (USB/low-battery/safe-stop share one close+rename, no double
    close/rename) + terminal manifest record (state=finalized), unmount
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
`wav_recovery.c`, `sha256.c`, `device_identity.c` portable subset,
`device_manifest.c`, `recorder_config.h`) has no ESP-IDF dependency. Host
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
and later-task scope boundaries (processed-ACK retention stays out; #45
rotation, #46 recovery/quarantine, and #47 identity/manifest are in
scope).
`test_device_manifest.py` locks Task #47 identity/manifest/integrity:
SHA-256 vectors (incremental, lowercase hex, file-hash matches PC
recomputation), UUIDv4 shape, `device.json`/manifest schema shape
(Spec #35 required fields, `schemaVersion=1`, unknown-major fail-closed,
deviceId-mismatch preservation), tmp+rename atomicity (crash preserves
the old manifest), CONFLICT on same recordingId with a different hash
(never overwrites), idempotent duplicate upserts, and writer wiring
(NVS ensure, `device.json` ensure, per-segment UUIDv4, rotation/terminal
`stage: manifest` records, recovered sync, metadata-only logging).
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
- Task #47 metadata evidence (synthetic payloads only, never real audio):
  `device.json` / `manifest.json` schema validation against the Spec #35
  required fields, hash comparison (`device SHA-256 == PC recomputation`)
  for finalized/recovered files, manifest power-loss test (kill during
  `manifest.tmp` write, old manifest intact; missing manifest + valid tmp
  promotes), no recordingId collisions across segments, and
  `stage: manifest` log lines (ok/error/conflict counts only).
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
