#!/usr/bin/env python3
"""Dependency-free tracked-content leakage scanner for M5Daylog.

The scanner fails closed when Git-tracked files cannot be enumerated/read and
never includes a matched sensitive value in a Finding or diagnostic message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

ALLOWLIST_FILE = ".security-scan-allowlist.json"
MAX_FILE_BYTES = 8 * 1024 * 1024

CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ino",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
}
CONFIG_SUFFIXES = CODE_SUFFIXES | {
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".properties", ".xml",
}
DOCUMENTATION_SUFFIXES = {".md", ".rst", ".adoc"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".aac", ".opus", ".ogg"}
FORBIDDEN_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore",
    ".pcap", ".pcapng", ".core", ".dump", ".dmp",
)
FORBIDDEN_NAME_PARTS = (
    "nvs-dump", "flash-dump", "ram-dump", "env-dump",
    "credential-export", "credential-backup", "session-export",
)
SAFE_FIXTURE_PREFIXES = (
    "fixtures/synthetic/", "fixtures/public/",
    "tests/fixtures/synthetic/", "tests/fixtures/public/",
)

# Construct token markers from fragments so this scanner does not match its own
# source code. Tests follow the same pattern.
_GITHUB_CLASSIC = "g" + "h" + "[pousr]_"
_GITHUB_FINE = "github" + "_pat_"
_HF = "h" + "f_"
_OPENAI = "s" + "k-"
_AWS = "A" + "KIA"
_SLACK = "x" + "ox[baprs]-"

SIGNATURE_PATTERNS = {
    "github-token": re.compile(r"(?:" + _GITHUB_CLASSIC + r"[A-Za-z0-9]{20,}|" + re.escape(_GITHUB_FINE) + r"[A-Za-z0-9_]{20,})"),
    "huggingface-token": re.compile(re.escape(_HF) + r"[A-Za-z0-9]{20,}"),
    "openai-token": re.compile(re.escape(_OPENAI) + r"[A-Za-z0-9_-]{20,}"),
    "aws-access-key": re.compile(re.escape(_AWS) + r"[0-9A-Z]{16}"),
    "slack-token": re.compile(re.escape(_SLACK) + r"[A-Za-z0-9-]{16,}"),
    "private-key": re.compile(r"-{5}BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-{5}"),
    "bearer-token": re.compile(r"Authorization\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._~+\-/=]{20,}", re.IGNORECASE),
    "credential-uri": re.compile(r"[a-z][a-z0-9+.-]{1,20}://[^\s/:@]+:[^\s/@]{8,}@", re.IGNORECASE),
}

_CREDENTIAL_NAME = (
    r"(?:password|passwd|passphrase|secret|api[_-]?key|token|pat|"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"webhook[_-]?secret|private[_-]?key|aws[_-]?secret[_-]?access[_-]?key|"
    r"hf[_-]?token|huggingface[_-]?token|session[_-]?(?:token|cookie))"
)
_CREDENTIAL_KEY = (
    r"(?:" + _CREDENTIAL_NAME + r"|(?P<credential_quote>[\"'])" + _CREDENTIAL_NAME + r"(?P=credential_quote))"
)
CREDENTIAL_LITERAL_PATTERN = re.compile(
    _CREDENTIAL_KEY + r"\s*[:=]\s*[\"'][^\"'\r\n]{8,}[\"']",
    re.IGNORECASE,
)

LOG_SINK_PATTERN = re.compile(
    r"(?:Serial\.(?:print|printf|println)|console\.(?:log|debug|info|warn|error)|"
    r"(?:print|printf|ESP_LOG[EWIDV]|LOG_[EWIDV]|logger\.(?:debug|info|warning|error|exception)))"
    r"\s*\([^\)\n]*(?:secret|password|passphrase|token|credential|authorization|cookie|"
    r"raw[_-]?audio|audio[_-]?bytes|transcript[_-]?text|speaker[_-]?embedding)",
    re.IGNORECASE,
)

KNOWN_RULES = frozenset({
    "forbidden-path", "large-file", "audio-file", "env-template-value",
    "credential-literal", "dangerous-log", *SIGNATURE_PATTERNS.keys(),
})
ALLOWLISTABLE_RULES = KNOWN_RULES - {"forbidden-path", "large-file", "env-template-value"}


class ScanError(RuntimeError):
    """Fatal scanner configuration/repository error."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str


@dataclass(frozen=True)
class AllowEntry:
    path: str
    rule: str
    kind: str
    file_sha256: str
    reason: str


def normalize_relative_path(raw: str) -> str:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ScanError(f"invalid repository-relative path: {raw!r}")
    return path.as_posix()


def is_safe_fixture_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in SAFE_FIXTURE_PREFIXES)


def load_allowlist(root: Path) -> list[AllowEntry]:
    path = root / ALLOWLIST_FILE
    if not path.exists():
        raise ScanError(f"required allowlist file is missing: {ALLOWLIST_FILE}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScanError(f"cannot read {ALLOWLIST_FILE}: {exc}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("entries"), list):
        raise ScanError(f"{ALLOWLIST_FILE} must contain version=1 and an entries array")

    entries: list[AllowEntry] = []
    expected = {"path", "rule", "kind", "file_sha256", "reason"}
    for index, raw in enumerate(payload["entries"]):
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ScanError(f"allowlist entry {index} must contain exactly: {', '.join(sorted(expected))}")
        entry = AllowEntry(
            path=normalize_relative_path(str(raw["path"])),
            rule=str(raw["rule"]),
            kind=str(raw["kind"]),
            file_sha256=str(raw["file_sha256"]).lower(),
            reason=str(raw["reason"]).strip(),
        )
        if entry.rule not in ALLOWLISTABLE_RULES:
            raise ScanError(f"allowlist entry {index} uses non-allowlistable rule: {entry.rule}")
        if entry.kind not in {"synthetic-fixture", "public-test-vector"}:
            raise ScanError(f"allowlist entry {index} has invalid kind: {entry.kind}")
        if not is_safe_fixture_path(entry.path):
            raise ScanError(f"allowlist entry {index} must target a designated fixture directory")
        if not re.fullmatch(r"[0-9a-f]{64}", entry.file_sha256):
            raise ScanError(f"allowlist entry {index} has invalid file_sha256")
        if not entry.reason:
            raise ScanError(f"allowlist entry {index} requires a non-empty reason")
        entries.append(entry)

    if len({(e.path, e.rule, e.file_sha256) for e in entries}) != len(entries):
        raise ScanError("duplicate allowlist entries are not permitted")
    return entries


def git_tracked_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError("cannot enumerate Git-tracked files; run inside a Git worktree") from exc
    paths = [normalize_relative_path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]
    if not paths:
        raise ScanError("Git reported no tracked files")
    return paths


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_path(path: str) -> list[Finding]:
    lowered = path.lower()
    name = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(path).suffix.lower()
    findings: list[Finding] = []

    if name.startswith(".env") and name != ".env.example":
        findings.append(Finding(path, 1, "forbidden-path", "local environment file must not be tracked"))
    suspicious_name = suffix not in DOCUMENTATION_SUFFIXES and any(part in lowered for part in FORBIDDEN_NAME_PARTS)
    if lowered.endswith(FORBIDDEN_SUFFIXES) or suspicious_name:
        findings.append(Finding(path, 1, "forbidden-path", "credential/dump/key-like file must not be tracked"))
    if suffix in AUDIO_SUFFIXES:
        findings.append(Finding(path, 1, "audio-file", "tracked audio requires exact synthetic/public fixture allowlist"))
    return findings


def scan_env_example(path: str, text: str) -> list[Finding]:
    if PurePosixPath(path).name != ".env.example":
        return []
    findings: list[Finding] = []
    for index, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in raw_line:
            findings.append(Finding(path, index, "env-template-value", "env template entry must use KEY= with empty value"))
            continue
        key, value = raw_line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()) or value.strip():
            findings.append(Finding(path, index, "env-template-value", "env template entry must use KEY= with empty value"))
    return findings


def scan_content(path: str, data: bytes) -> list[Finding]:
    if len(data) > MAX_FILE_BYTES:
        return [Finding(path, 1, "large-file", f"tracked file exceeds {MAX_FILE_BYTES} bytes and is not scanned")]
    text = data.decode("utf-8", errors="replace")
    suffix = PurePosixPath(path).suffix.lower()
    findings = scan_env_example(path, text)

    for rule, pattern in SIGNATURE_PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append(Finding(path, line_number(text, match.start()), rule, "high-confidence credential/private-key signature"))

    if suffix in CONFIG_SUFFIXES or PurePosixPath(path).name.startswith(".env"):
        for match in CREDENTIAL_LITERAL_PATTERN.finditer(text):
            findings.append(Finding(path, line_number(text, match.start()), "credential-literal", "credential-like literal assignment"))

    if suffix in CODE_SUFFIXES:
        for match in LOG_SINK_PATTERN.finditer(text):
            findings.append(Finding(path, line_number(text, match.start()), "dangerous-log", "logging call appears to include protected data"))
    return findings


def apply_allowlist(findings: Iterable[Finding], file_hashes: dict[str, str], entries: Iterable[AllowEntry]) -> tuple[list[Finding], list[AllowEntry]]:
    entry_map = {(e.path, e.rule, e.file_sha256): e for e in entries}
    used: set[tuple[str, str, str]] = set()
    remaining: list[Finding] = []
    for finding in findings:
        digest = file_hashes.get(finding.path, "")
        key = (finding.path, finding.rule, digest)
        if key in entry_map:
            used.add(key)
        else:
            remaining.append(finding)
    stale = [entry for key, entry in entry_map.items() if key not in used]
    return remaining, stale


def scan_repository(root: Path) -> tuple[list[Finding], list[AllowEntry]]:
    entries = load_allowlist(root)
    findings: list[Finding] = []
    hashes: dict[str, str] = {}
    for relative in git_tracked_files(root):
        full_path = root / relative
        findings.extend(scan_path(relative))
        try:
            data = full_path.read_bytes()
        except OSError as exc:
            raise ScanError(f"cannot read tracked file {relative}: {exc}") from exc
        hashes[relative] = file_sha256(data)
        findings.extend(scan_content(relative, data))
    return apply_allowlist(findings, hashes, entries)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan tracked repository files for credential/private-data leakage")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        findings, stale = scan_repository(args.root.resolve())
    except ScanError as exc:
        print(f"[security:scan] ERROR: {exc}", file=sys.stderr)
        return 2

    for entry in stale:
        print(f"[security:scan] ERROR: stale allowlist entry {entry.path} [{entry.rule}]", file=sys.stderr)
    for finding in findings:
        print(f"[security:scan] {finding.path}:{finding.line}: [{finding.rule}] {finding.message}", file=sys.stderr)

    if findings or stale:
        print(f"[security:scan] FAILED: {len(findings)} finding(s), {len(stale)} stale allowlist entry/entries", file=sys.stderr)
        return 1
    print("[security:scan] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
