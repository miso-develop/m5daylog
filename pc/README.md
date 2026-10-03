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
