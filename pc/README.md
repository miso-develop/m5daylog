# M5Daylog PC PoC (`pc/`)

Python 3.11 / `uv` based local-first CLI. Task #43 provides the SQLite scaffold; Task #51 adds manifest-based Device inspection and incremental raw-audio sync using the canonical Integration-owned schemas under `../contracts/`.

## Windows 10 baseline

Prerequisites: Python 3.11.x and `uv`.

```powershell
cd pc
uv sync
uv run m5daylog --help
uv run m5daylog init
uv run python -m unittest discover -s tests -v
```

Default state DB: `state\m5daylog.db` relative to the working directory. `pc\state\` is local-only and ignored.

## Device inspection

The Device filesystem is treated as read-only. `device.json` is validated against the canonical `contracts/device.schema.json` before identification.

```powershell
uv run m5daylog device E:\
```

When running outside the repository source layout, the canonical contract directory can be supplied explicitly:

```powershell
uv run m5daylog device E:\ --contracts-dir C:\src\m5daylog\contracts
```

## Incremental sync

`sync` validates both `device.json` and `manifest.json`, requires their `deviceId` values to match, rejects duplicate recording IDs and unsafe source paths before copying, then streams each required recording through `<recordingId>.wav.partial`.

A partial is always opened from byte 0; it is never appended/resumed. Size and SHA-256 are verified before no-clobber publication to:

```text
<library>\YYYY\MM\DD\raw\<recordingId>.wav
```

`YYYY\MM\DD` is the calendar date represented by the recording's own `startedAt` offset; no timezone conversion is applied.

```powershell
uv run m5daylog sync E:\
```

Default library: `%USERPROFILE%\Documents\M5Daylog`.

Explicit local paths:

```powershell
uv run m5daylog sync E:\ `
  --library-root C:\M5Daylog `
  --db C:\M5DaylogState\m5daylog.db
```

Or use a state root:

```powershell
$env:M5DAYLOG_DATA_DIR = "$env:LOCALAPPDATA\M5Daylog"
uv run m5daylog sync E:\ --library-root C:\M5Daylog
```

The summary prints counts only, for example `copied`, `reused`, and `conflicts`; it does not print audio content or manifest filenames. Exit code `2` means at least one conflict was detected. A conflict never overwrites/deletes an existing final raw WAV.

## Recovery / idempotency semantics

- Second sync of unchanged content copies zero bytes and converges to one SQLite row per `recordingId`.
- A stale `.partial` is truncated and recopied from byte 0.
- A matching final WAV with a missing/stale DB row is hash-verified and the DB converges without recopying.
- An existing final WAV with a size/hash mismatch is marked `CONFLICT` and remains byte-for-byte unchanged.
- The DB is marked `SYNCED` only after the final file exists and passes size/SHA-256 verification.
- Device-side `acks/` or any other Device file is not written by Task #51; acknowledgement ownership belongs to the later ack task.

## Canonical contracts

PC runtime validation reads the repository canonical schemas; it does not maintain a second field/schema definition. The validator fails closed if the schema gains an unsupported keyword so contract changes cannot be silently ignored.

Shared schemas/fixtures remain owned by Integration:

```text
contracts/device.schema.json
contracts/manifest.schema.json
fixtures/device/**
fixtures/manifest/**
```

## Layout

```text
pc/
  pyproject.toml
  uv.lock
  README.md
  src/m5daylog/
    __init__.py
    __main__.py
    cli.py
    db.py
    sync.py
  tests/
    test_cli_db.py
    test_sync.py
  state/            # generated locally, ignored
```

## Security / privacy

- No real credentials, tokens, secrets, recordings, transcripts, speaker data, or daily logs are committed as tests/fixtures.
- Sync never uploads protected content; no analytics, telemetry, or remote error reporting is introduced.
- Device metadata and manifest content are not dumped on errors.
- Tests use synthetic IDs, metadata, paths, and byte payloads only.

## Recording-scoped VAD (Task #54; implementation in progress)

VAD reads a single previously synced 16-kHz mono signed-PCM WAV. It never
modifies that WAV. It produces the deterministic UTF-8 artifact
`YYYY/MM/DD/vad/<recording_id>.jsonl`. The initial meta line is present even
when no speech is found; subsequent lines use the original WAV sample timeline.

```powershell
cd pc
uv lock --python 3.11
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen m5daylog vad <recording-uuid> --library-root "$HOME\Documents\M5Daylog" --db "$env:LOCALAPPDATA\M5Daylog\state\m5daylog.db"
```

**Verification status:** `uv.lock` is now resolved for Python 3.11,
and the expected ONNX SHA-256 has been pinned from the official Silero 6.2.3
wheel. The runtime checks that hash before loading. The public PC CI checks
the frozen dependency graph, synthetic tests, and real CPU ONNX silence
inference. These checks do **not** establish the EV-018 acoustic gates on
actual M5Daylog recordings, which require separate annotated local fixtures.

The stage checks SYNCED state, canonical local path, source size/hash and PCM
format; validates/reuses an exact-source, exact-producer artifact without
inference; and atomically publishes fresh JSONL via fsynced temporary file.
A final artifact published before an interrupted SQLite commit can be
validated and adopted on retry. Source audio and transcripts are never logged.
STT (#55) must consume only a successful, current schema-v1 VAD artifact.


## Offline RAW/DC diagnostic comparator (Issue #54, section 16)

The production `m5daylog vad` command remains **RAW v1**: its original
fingerprint, JSONL schema, SQLite state and STT input contract are unchanged.
The explicit `vad-diagnose` command compares raw PCM to whole-recording
DC-mean-subtracted PCM using the identical pinned Silero parameters.
It is **analysis-only**: no canonical artifact or database writes, no
raw WAV edits, no off-host transfers, and no STT consumption.

Local PowerShell (from pc/, with frozen Python 3.11 dependencies):

```powershell
uv run --frozen m5daylog vad-diagnose <recording-uuid> --db <local-db> --library-root <local-library> --code-revision <verified-40-hex-HEAD>
```

Optional local annotation for measured recall and false positives:

```json
{
  "fixture_id": "local-fixture",
  "category": "quiet",
  "source_sha256": "<full WAV SHA-256>",
  "total_samples": 16000,
  "reference": [[100, 2800], [6500, 11000]]
}
```

Pass `--annotation <local-reference.json>`; the sample count and WAV hash
must match. Keep recordings, annotations and personal observations local.
The code-revision option is an operator-supplied exact revision **label**,
not proof that installed code matches the revision. Evidence identifies
RAW_v1 and dc_global_mean_v1, SHA-256, sample count, signed sum, mean PCM
LSB, temporary clipping, 60s drift-window means, model/lock/version
fingerprints and range digests. The CLI does not emit speech boundaries,
raw samples, transcripts, or filesystem paths; nevertheless review any
evidence before public publication.

Transform order: all signed PCM16 values are summed exactly in bounded
blocks; their whole-recording mean is subtracted in float64, divided by
32768, clipped to [-1,1], then cast to float32 for inference. No gain,
resample, inserted samples, trim, or per-window mean reset. Both ranges
refer to the **original** WAV sample timeline. Single WAV inputs longer
than 31 minutes receive DIAGNOSTIC_RESOURCE_BLOCKED (exit 2), not a
product VAD failure; other invalid inputs fail closed (exit 1).

EV-018 remains **NOT_RUN** until qualified annotated silence, device-noise,
quiet, 1m, and noisy real-audio fixtures are evaluated with independent
acoustic evidence for both variants. Increased detected duration alone is
not acceptance. Production DC compensation would require a further
Specification approval, algorithm bump and independent Review/Security/
Human Gate. Draft PR #110 is not authorized to merge.
