# M5Daylog PC PoC (`pc/`)

Python 3.12 / `uv` based local-first v0.1 raw-WAV CLI. The Python 3.11 Torch/ONNX AI pipeline is deferred and separately qualified (#54). Task #43 provides the SQLite scaffold; Task #51 adds manifest-based Device inspection and incremental raw-audio sync using the canonical Integration-owned schemas under `../contracts/`.

## Windows 10 baseline

Prerequisites: CPython 3.12.x and `uv` (v0.1 raw-only). Lock freshness and exact-head Python are checked by PC CI.

```powershell
cd pc
uv lock --check
uv sync --frozen
uv run --frozen python --version
uv run --frozen m5daylog --help
uv run --frozen m5daylog init
uv run --frozen python -m unittest discover -s tests -v
```

The `uv lock --check` step validates lock freshness; `--frozen` alone does not. A successful CLI smoke is not a Windows USB/Device Human Gate.

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

## Windows release orchestration (Task #89 / Decision D-031)

The existing \`sync\` command remains **read-only and does not release USB ownership**.
The separate, explicit \`sync-release\` operation first runs the same durable
sync, then seals ordinary MSC/CDC I/O and invokes the canonical host release:

\`\`\`powershell
cd pc
uv run m5daylog sync-release F:\ --cdc-port COM5
\`\`\`

**Safety:** This command requests the Device to terminate its USB MSC/CDC
session after host volume lock/dismount. Do not use against an unknown Device,
a shared drive, or while another application is using the volume. Passing a
different drive letter or COM port can cause a failed or indeterminate release.
Only perform physical acceptance after independent review and Human Gate
authorization. The CLI does not invoke Windows taskbar removal, PnP eject, or
SCSI pass-through as an alternative authority.

The implementation validates \`device.json\` with the canonical schema, uses
the #51 copy/hash/SQLite barrier, freezes and revalidates the exact Volume GUID,
single DiskNumber, and unique removable devnode ancestry, seals in-process
ordinary I/O, drains its handles, holds \`FSCTL_LOCK_VOLUME\` through
\`FSCTL_DISMOUNT_VOLUME\` and the CDC response, and sends one canonical
\`RELEASE_STORAGE(releaseAttemptId)\`. Release succeeds only with a matching
\`accepted=true\` response **and** disappearance of the exact disk interface
and inaccessible frozen Volume GUID. Dismount or physical detach alone is not
release proof.

\`sync-release\` returns exit 0 for \`SYNC_COMPLETE_RELEASED\`, 2 for
\`SYNC_INTERRUPTED\` (including #51 conflicts), 3 for
\`SYNC_COMPLETE_RELEASE_FAILED\`, and 1 for CLI/host setup failures. Its output
contains only status, failure class, and release attempt ID. CDC response
loss is \`INDETERMINATE\`; never auto-resend a new release attempt after
possible acceptance. A release failure never rolls back already verified raw
files or SQLite state. Charging while connected is Device lifecycle behavior,
not a claim of host-side safe physical power-off.

The #52 pre-release eligible-ACK flush and scheduled pre-release CDC mutation
(e.g. SET_TIME) are **injected coordination seams**, not automatically generated
by this initial CLI. Callers that require them must invoke those hooks before
the session gate seals. In particular, do not describe a successful CLI exit as
proof that an outstanding scheduled mutation or ACK has been delivered.
No shared cross-process admission gate is provided; ordinary M5Daylog I/O must
go through the same \`UsbSessionIoGate\` instance for the in-process guarantee.

Platform-neutral coordinator and mockable adapter regression suite:

\`\`\`powershell
cd pc
uv run python -m unittest discover -s tests -v
\`\`\`

The fake Win32 API tests do not substitute for a stock-Windows live device
Human Gate proving lock+dismount, CDC accepted response, exact target removal,
Device backend quiescence and durable \`SHUTDOWN_ARMED\`. Native Win32
implementation and any subsequent controller/firmware changes require that
independent acceptance.

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
