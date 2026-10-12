"""Recording-scoped, fail-closed Silero VAD stage (Issue #54).

Canonical coordinates are original 16-kHz PCM samples. No raw data is
modified, emitted to logs, or uploaded. VAD artifacts are durable, not cache.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import io
import json
import os
import re
import sqlite3
import stat
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .sync import resolve_library_root

PRODUCER = "m5daylog.vad"
SCHEMA_VERSION = 1
ALGORITHM_VERSION = 1
MERGE_VERSION = 1
SAMPLE_RATE = 16000
# SHA-256 measured from the PyPI silero-vad==6.2.3 wheel in PR #110 CI.
# Runtime re-hashes the package asset before loading; mismatch fails closed.
EXPECTED_MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"

PARAMS = {
    "threshold": 0.5,
    "neg_threshold": 0.35,
    "min_speech_duration_ms": 250,
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 300,
    "window_size_samples": 512,
    "max_speech_duration_s": "inf",
    "sampling_rate": SAMPLE_RATE,
    "return_seconds": False,
    "min_silence_at_max_speech": 98,
    "use_max_poss_sil_at_max_speech": True,
    "time_resolution": 1,
    "merge_gap_samples": 0,
}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class VadError(ValueError):
    """Safe diagnostic: never include protected WAV bytes or text."""


class VadBusy(VadError):
    pass


@dataclass(frozen=True)
class VadResult:
    status: str
    ranges: int
    sha256: str


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def metadata(runtime: object) -> dict:
    return {
        "name": "silero-vad",
        "package_version": runtime.silero_version,
        "torch_version": runtime.torch_version,
        "runtime": "onnxruntime",
        "runtime_version": runtime.onnxruntime_version,
        "model_file": "silero_vad.onnx",
        "model_sha256": runtime.model_sha256,
        "opset": 16,
        "provider": "CPUExecutionProvider",
    }


def fingerprint(model: dict) -> str:
    config = {
        "artifact_schema_version": SCHEMA_VERSION,
        "vad_algorithm_version": ALGORITHM_VERSION,
        "merge_algorithm_version": MERGE_VERSION,
        "model": model,
        "params": PARAMS,
    }
    return "v1:" + hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


class SileroCPU:
    """Load the package-bundled, checked model; never download at runtime."""

    def __init__(self) -> None:
        if EXPECTED_MODEL_SHA256 is None:
            raise VadError("model digest has not been pinned")
        import onnxruntime
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad

        self.silero_version = importlib.metadata.version("silero-vad")
        self.torch_version = importlib.metadata.version("torch")
        self.onnxruntime_version = importlib.metadata.version("onnxruntime")
        if self.silero_version != "6.2.3" or self.onnxruntime_version != "1.30.0":
            raise VadError("model/runtime version mismatch")
        asset = importlib.resources.files("silero_vad.data").joinpath("silero_vad.onnx")
        self.model_sha256 = hashlib.sha256(asset.read_bytes()).hexdigest()
        if self.model_sha256 != EXPECTED_MODEL_SHA256:
            raise VadError("model asset SHA-256 mismatch")
        self.model = load_silero_vad(onnx=True, opset_version=16, sequence=False)
        if self.model.session.get_providers() != ["CPUExecutionProvider"]:
            raise VadError("unexpected ONNX execution provider")
        self._infer = get_speech_timestamps
        self._torch = torch

    def detect(self, samples: bytes) -> list[dict]:
        import numpy as np
        pcm = np.frombuffer(samples, dtype="<i2")
        audio = self._torch.from_numpy((pcm.astype(np.float32) / 32768.0).copy())
        args = dict(PARAMS)
        args["max_speech_duration_s"] = float("inf")
        args.pop("merge_gap_samples")
        return self._infer(audio, self.model, **args)


def normalize_ranges(raw: list[dict], total_samples: int) -> list[tuple[int, int]]:
    ranges = []
    if not isinstance(raw, list):
        raise VadError("invalid Silero range output")
    for entry in raw:
        if (not isinstance(entry, dict) or type(entry.get("start")) is not int
                or type(entry.get("end")) is not int):
            raise VadError("invalid Silero range output")
        start = max(0, entry["start"])
        end = min(total_samples, entry["end"])
        if start >= end:
            raise VadError("invalid Silero range output")
        ranges.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _safe_date(started_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(started_at[:-1] + "+00:00" if started_at.endswith(("Z", "z")) else started_at)
    except (ValueError, AttributeError):
        raise VadError("invalid started_at") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VadError("invalid started_at")
    return parsed.date().isoformat()


def _inside(root: Path, path: Path) -> bool:
    return path.is_relative_to(root)


def _source(row: sqlite3.Row, root: Path) -> tuple[bytes, int, str, Path]:
    rid = row["recording_id"]
    if not isinstance(rid, str) or not _UUID.fullmatch(rid):
        raise VadError("invalid recording identity")
    if row["sync_status"] != "SYNCED":
        raise VadError("recording is not SYNCED")
    date = _safe_date(row["started_at"])
    expected = root.joinpath(*date.split("-"), "raw", rid + ".wav")
    path = Path(row["local_path"] or "")
    if path != expected and path.resolve(strict=False) != expected.resolve(strict=False):
        raise VadError("noncanonical raw path")
    if path.name.endswith(".partial") or path.suffix.lower() != ".wav":
        raise VadError("invalid raw file name")
    try:
        resolved = path.resolve(strict=True)
        if not _inside(root, resolved) or not resolved.is_file():
            raise VadError("raw file not in local library")
        raw = resolved.read_bytes()
    except OSError:
        raise VadError("raw input missing") from None
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != row["size_bytes"] or digest != row["source_sha256"]:
        raise VadError("raw source integrity mismatch")
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise VadError("unsupported WAV format")
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if (wav.getnchannels() != 1 or wav.getsampwidth() != 2
                    or wav.getframerate() != SAMPLE_RATE or wav.getcomptype() != "NONE"):
                raise VadError("unsupported WAV format")
            total = wav.getnframes()
            pcm = wav.readframes(total)
            if len(pcm) != total * 2:
                raise VadError("truncated WAV frames")
            declared_ms = row["duration_ms"]
            if declared_ms is not None:
                if (type(declared_ms) is not int or declared_ms < 0
                        or abs(declared_ms * SAMPLE_RATE - total * 1000)
                           > SAMPLE_RATE):
                    raise VadError("manifest/WAV duration mismatch")
    except (wave.Error, EOFError, OSError):
        raise VadError("unsupported/corrupt WAV format") from None
    return pcm, total, digest, resolved


def _artifact_bytes(row: sqlite3.Row, model: dict, version: str,
                    source_sha: str, total: int, ranges: list[tuple[int, int]]) -> bytes:
    meta = {
        "type": "meta", "schema_version": SCHEMA_VERSION,
        "recording_id": row["recording_id"], "source_sha256": source_sha,
        "started_at": row["started_at"], "sample_rate": SAMPLE_RATE,
        "total_samples": total, "producer": PRODUCER,
        "producer_version": version, "model": model,
        "params": {
            "threshold": PARAMS["threshold"], "neg_threshold": PARAMS["neg_threshold"],
            "min_speech_ms": PARAMS["min_speech_duration_ms"],
            "min_silence_ms": PARAMS["min_silence_duration_ms"],
            "speech_pad_ms": PARAMS["speech_pad_ms"],
            "window_size_samples": PARAMS["window_size_samples"],
            "merge_gap_samples": 0,
            "max_speech_duration_s": "inf",
            "sampling_rate": SAMPLE_RATE,
            "return_seconds": False,
            "min_silence_at_max_speech": PARAMS["min_silence_at_max_speech"],
            "use_max_poss_sil_at_max_speech": PARAMS["use_max_poss_sil_at_max_speech"],
            "time_resolution": PARAMS["time_resolution"],
        },
    }
    records = [meta]
    for index, (start, end) in enumerate(ranges):
        records.append({
            "type": "speech", "index": index, "start_sample": start,
            "end_sample": end, "start_sec": start / SAMPLE_RATE,
            "end_sec": end / SAMPLE_RATE,
        })
    return ("\n".join(canonical_json(item) for item in records) + "\n").encode("utf-8")


def validate_jsonl(data: bytes, rid: str, source_sha: str, version: str,
                  total_samples: int | None = None) -> int:
    try:
        if not data.endswith(b"\n"):
            raise ValueError("missing newline")
        lines = data.decode("utf-8").splitlines()
        records = [json.loads(line) for line in lines]
        head = records[0]
        if not isinstance(head, dict):
            raise ValueError("bad metadata")
        if (head.get("type") != "meta" or head.get("schema_version") != 1
                or head.get("recording_id") != rid
                or head.get("source_sha256") != source_sha
                or not isinstance(head.get("model"), dict)
                or not isinstance(head.get("params"), dict)
                or head.get("started_at") is None
                or head.get("producer") != PRODUCER
                or head.get("producer_version") != version
                or head.get("sample_rate") != SAMPLE_RATE
                or type(head.get("total_samples")) is not int):
            raise ValueError("bad meta identity")
        n = head["total_samples"]
        if ((total_samples is not None and n != total_samples) or n < 0
                or any(canonical_json(record) != line for record, line in zip(records, lines))):
            raise ValueError("noncanonical JSONL")
        previous_end = -1
        for i, obj in enumerate(records[1:]):
            a, b = obj["start_sample"], obj["end_sample"]
            if (obj.get("type") != "speech" or type(obj.get("index")) is not int
                    or obj["index"] != i or type(a) is not int or type(b) is not int
                    or not (0 <= a < b <= n) or a <= previous_end
                    or obj["start_sec"] != a / SAMPLE_RATE
                    or obj["end_sec"] != b / SAMPLE_RATE):
                raise ValueError("invalid speech range")
            previous_end = b
        return len(records) - 1
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, UnicodeError, json.JSONDecodeError):
        raise VadError("invalid VAD artifact") from None


@contextmanager
def _recording_lock(path: Path):
    """OS-held advisory lock: crash releases lock even if lexical file remains."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise VadError("unsafe VAD lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise VadError("unsafe VAD lock")
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise VadBusy("VAD recording already active") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise VadBusy("VAD recording already active") from None
        yield
    finally:
        os.close(fd)


def _publish(path: Path, data: bytes, rid: str, source: str, version: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(path) + ".tmp")
    if temp.is_dir() and not temp.is_symlink():
        raise VadError("unsafe VAD temporary entry")
    if temp.exists() or temp.is_symlink():
        temp.unlink()  # unlink lexical entry, never follow its target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        validate_jsonl(temp.read_bytes(), rid, source, version)
        os.replace(temp, path)
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        if temp.exists() or temp.is_symlink():
            temp.unlink()


def _save_success(db: sqlite3.Connection, row: sqlite3.Row, path: Path,
                  digest: str, version: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db:
        db.execute("""INSERT INTO artifacts
            (artifact_id,recording_id,target_date,artifact_type,path,sha256,
             producer,producer_version,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              target_date=excluded.target_date,path=excluded.path,
              sha256=excluded.sha256,producer=excluded.producer,
              producer_version=excluded.producer_version,created_at=excluded.created_at""",
            ("vad:" + row["recording_id"], row["recording_id"],
             _safe_date(row["started_at"]), "vad", str(path), digest,
             PRODUCER, version, now))
        db.execute("UPDATE recordings SET vad_status='SUCCEEDED' WHERE recording_id=?",
                   (row["recording_id"],))
        db.execute("""UPDATE jobs SET status='SUCCEEDED',completed_at=?,
                      last_error=NULL WHERE job_id=?""", (now, "vad:" + row["recording_id"]))



def run_vad(recording_id: str, *, db_path: str | Path,
            library_root: str | Path | None = None,
            runtime: object | None = None) -> VadResult:
    """Run one recording with OS-held cross-process admission and safe state."""
    if not _UUID.fullmatch(recording_id):
        raise VadError("invalid recording identity")
    root = resolve_library_root(library_root).resolve(strict=True)
    db = sqlite3.connect(str(db_path), timeout=0.1)
    db.row_factory = sqlite3.Row
    job_id = "vad:" + recording_id

    def fail_safe(exc: Exception) -> None:
        """Invalidate any previously successful VAD before STT can consume it."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            with db:
                db.execute(
                    "UPDATE recordings SET vad_status='FAILED' WHERE recording_id=?",
                    (recording_id,),
                )
                db.execute("""INSERT INTO jobs
                    (job_id,job_type,recording_id,status,attempts,created_at,last_error)
                    VALUES (?,'VAD',?,'FAILED',0,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET
                    status='FAILED',last_error=excluded.last_error""",
                    (job_id, recording_id, now, type(exc).__name__))
        except sqlite3.Error:
            pass  # Never cover the original safe failure with logging failure.

    try:
        row = db.execute("SELECT * FROM recordings WHERE recording_id=?",
                         (recording_id,)).fetchone()
        if row is None:
            raise VadError("unknown recording")
        date = _safe_date(row["started_at"])
        out = root.joinpath(*date.split("-"), "vad", recording_id + ".jsonl")
        if not _inside(root, out.parent.resolve(strict=False)):
            raise VadError("VAD output path escapes library")
        with _recording_lock(out.with_suffix(".lock")):
            try:
                pcm, total, source_sha, resolved = _source(row, root)
                model_runtime = runtime if runtime is not None else SileroCPU()
                model = metadata(model_runtime)
                if (model["package_version"] != "6.2.3"
                        or model["runtime_version"] != "1.30.0"
                        or not _SHA.fullmatch(model["model_sha256"])):
                    raise VadError("invalid VAD runtime identity")
                version = fingerprint(model)
            except Exception as exc:
                fail_safe(exc)
                raise

            existing = db.execute("SELECT * FROM artifacts WHERE artifact_id=?",
                                  (job_id,)).fetchone()
            valid = None
            if out.is_file() and not out.is_symlink():
                try:
                    data = out.read_bytes()
                    valid = (validate_jsonl(
                        data, recording_id, source_sha, version, total),
                        hashlib.sha256(data).hexdigest())
                except VadError:
                    valid = None
            if (valid is not None and existing is not None
                    and row["vad_status"] == "SUCCEEDED"
                    and existing["path"] == str(out)
                    and existing["sha256"] == valid[1]
                    and existing["producer"] == PRODUCER
                    and existing["producer_version"] == version):
                return VadResult("SKIPPED", valid[0], valid[1])

            started = datetime.now(timezone.utc).isoformat()
            with db:
                db.execute("""INSERT INTO jobs
                    (job_id,job_type,recording_id,status,attempts,created_at,started_at)
                    VALUES (?,'VAD',?,'RUNNING',1,?,?)
                    ON CONFLICT(job_id) DO UPDATE SET
                      status='RUNNING',attempts=COALESCE(jobs.attempts,0)+1,
                      started_at=excluded.started_at,completed_at=NULL,last_error=NULL""",
                    (job_id, recording_id, started, started))
                db.execute("UPDATE recordings SET vad_status='RUNNING' WHERE recording_id=?",
                           (recording_id,))
            try:
                if valid is not None:
                    _save_success(db, row, out, valid[1], version)
                    return VadResult("RECOVERED", valid[0], valid[1])
                detected = model_runtime.detect(pcm)
                ranges = normalize_ranges(detected, total)
                payload = _artifact_bytes(row, model, version, source_sha, total, ranges)
                if hashlib.sha256(resolved.read_bytes()).hexdigest() != source_sha:
                    raise VadError("source changed during VAD")
                if not _inside(root, out.parent.resolve(strict=False)):
                    raise VadError("VAD output path escapes library")
                digest = _publish(out, payload, recording_id, source_sha, version)
                _save_success(db, row, out, digest, version)
                return VadResult("SUCCEEDED", len(ranges), digest)
            except Exception as exc:
                fail_safe(exc)
                raise
    finally:
        db.close()
