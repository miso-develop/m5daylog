# M5Daylog PC scaffold (`pc/`)

PoC Python CLI / SQLite scaffold for Task #43 (`IM-006`).
Parent Map #1, related Decisions #16 / #22, Specs #34 / #38.

## Scope (#43)

- `pyproject.toml` + `uv.lock` (Python 3.11, `uv`)
- CLI entrypoint `m5daylog` with `--help` / `--version` and `init`
- SQLite bootstrap at `state/m5daylog.db` (default)
- `unittest` coverage with synthetic data only

Out of scope: VAD, STT, diarization, real device sync, `device` /
`sync` / `process` / `build-day` / `run` implementations. Those arrive
in #51 and follow-ups.

## Windows 10 baseline (PoC)

Prerequisites: Python 3.11.x and `uv` on Windows 10.

```powershell
cd pc
uv sync
uv run m5daylog --help
uv run m5daylog init
uv run python -m unittest discover -s tests -v
```

Default database: `state\m5daylog.db` relative to the working directory.
`pc\state\` is local-only and ignored by `pc/.gitignore`; never commit
the generated `.db` file.

Optional data-root override (concrete machine paths stay local-only):

```powershell
$env:M5DAYLOG_DATA_DIR = "$env:LOCALAPPDATA\M5Daylog"
uv run m5daylog init
# -> <data-dir>\state\m5daylog.db
```

Explicit path:

```powershell
uv run m5daylog init --db C:\work\m5daylog-state\m5daylog.db
uv run m5daylog init --data-dir C:\work\m5daylog-data
```

`python -m m5daylog --help` works as an equivalent entrypoint.

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
  tests/
    test_cli_db.py
  state/            # generated at runtime, ignored
```

## Security / privacy

- No real credential, token, secret, or private key handling.
- No raw audio, transcript, speaker, or daily-log content in code,
  tests, logs, or fixtures. Tests use synthetic IDs/paths only.
- `init` prints allowlisted metadata (database path, table names).
  Error output reports the failure class without sensitive input.
- Network upload, analytics, telemetry, and remote error reporting
  are not part of this scaffold.
