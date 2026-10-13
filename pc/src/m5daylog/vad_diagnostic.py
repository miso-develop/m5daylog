"""Read-only RAW/DC-offset VAD A/B diagnostics; never a production VAD producer.

Specification: #54 section 16. The diagnostic must not call run_vad, publish
vad.jsonl, update recordings/jobs/artifacts, or supply ranges to STT.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .sync import resolve_library_root
from .vad import (
    SAMPLE_RATE, SileroCPU, VadError, _SHA, _UUID, _source,
    canonical_json, fingerprint, metadata, normalize_ranges,
)
from .vad_eval import FixtureMetrics, aggregate_speech, score_fixture

RAW = "RAW_v1"
DC = "dc_global_mean_v1"
TRANSFORM_VERSION = 1
BLOCK_SAMPLES = 65_536
DRIFT_WINDOW_SAMPLES = SAMPLE_RATE * 60
# A 30-minute rotation with 60 seconds of slack. Reject anomalously long
# individual WAVs before _source loads them; this is not a daily-audio limit.
MAX_DIAGNOSTIC_SAMPLES = SAMPLE_RATE * 31 * 60
MAX_WAV_HEADER_BYTES = 1_048_576


class DiagnosticResourceBlocked(VadError):
    """The analysis resource envelope was exceeded; no product gate verdict."""


@dataclass(frozen=True)
class DiagnosticVariant:
    name: str
    ranges: tuple[tuple[int, int], ...]
    ranges_sha256: str
    detected_samples: int
    metrics: FixtureMetrics | None

    def summary(self, total_samples: int) -> dict:
        result = {
            "name": self.name,
            "range_count": len(self.ranges),
            "ranges_sha256": self.ranges_sha256,
            "detected_samples": self.detected_samples,
            "detected_seconds": self.detected_samples / SAMPLE_RATE,
            "quality": "NOT_RUN",
        }
        if self.metrics is not None:
            m = self.metrics
            nonspeech = total_samples - m.reference_samples
            result["quality"] = {
                "reference_samples": m.reference_samples,
                "recovered_samples": m.recovered_samples,
                "recall": m.recall,
                "onset_median_ms": m.onset_median_ms,
                "onset_p95_ms": m.onset_p95_ms,
                "missed_utterances": m.missed_utterances,
                "false_positive_samples": m.false_positive_samples,
                "false_positive_seconds": m.false_positive_samples / SAMPLE_RATE,
                "false_positive_ratio_over_nonspeech": (
                    m.false_positive_samples / nonspeech if nonspeech else None
                ),
            }
        return result


@dataclass(frozen=True)
class DiagnosticResult:
    source_sha256: str
    total_samples: int
    sample_sum: int
    mean_pcm_lsb: float | None
    clipped_samples: int
    drift_window_means: tuple[float, ...]
    producer_version: str
    model_sha256: str
    lock_sha256: str | None
    code_revision: str | None
    annotation_sha256: str | None
    raw: DiagnosticVariant
    dc: DiagnosticVariant

    def summary(self) -> dict:
        return {
            "status": "DIAGNOSTIC_ONLY_NOT_EV018_ACCEPTED",
            "source_sha256": self.source_sha256,
            "total_samples": self.total_samples,
            "sample_sum": self.sample_sum,
            "mean_pcm_lsb": self.mean_pcm_lsb,
            "clipped_samples": self.clipped_samples,
            "drift_window_means": list(self.drift_window_means),
            "production_producer_version_unmodified": self.producer_version,
            "model_sha256": self.model_sha256,
            "lock_sha256": self.lock_sha256,
            "code_revision": self.code_revision,
            "transform": {
                "name": DC,
                "version": TRANSFORM_VERSION,
                "order": "float64(int16-mean)/32768 then clip[-1,1] then float32",
                "gain": 1,
            },
            "annotation_sha256": self.annotation_sha256,
            "variants": [
                self.raw.summary(self.total_samples),
                self.dc.summary(self.total_samples),
            ],
        }


def _blocks(pcm: bytes, *, block_samples: int = BLOCK_SAMPLES):
    """Bound integer accumulation to blocks; never int16-accumulate a file."""
    if block_samples <= 0 or len(pcm) % 2:
        raise VadError("invalid diagnostic PCM")
    import numpy as np

    view = memoryview(pcm)
    for begin in range(0, len(view), block_samples * 2):
        yield np.frombuffer(view[begin:begin + block_samples * 2], dtype="<i2")


def _stat(pcm: bytes) -> tuple[int, int, float | None, tuple[float, ...]]:
    """Exact int64 block sums and fixed 60-second drift windows."""
    import numpy as np

    if len(pcm) % 2:
        raise VadError("invalid diagnostic PCM")
    n = len(pcm) // 2
    total_sum = 0
    window_sums: list[int] = []
    window_counts: list[int] = []
    for offset in range(0, n, BLOCK_SAMPLES):
        part = np.frombuffer(memoryview(pcm)[2 * offset:2 * min(n, offset + BLOCK_SAMPLES)],
                             dtype="<i2")
        # Split at 60-second boundaries even when the block crosses a boundary.
        local = 0
        while local < len(part):
            position = offset + local
            count = min(len(part) - local, DRIFT_WINDOW_SAMPLES -
                        position % DRIFT_WINDOW_SAMPLES)
            signed_sum = int(part[local:local + count].sum(dtype=np.int64))
            total_sum += signed_sum
            window = position // DRIFT_WINDOW_SAMPLES
            if window == len(window_sums):
                window_sums.append(0)
                window_counts.append(0)
            window_sums[window] += signed_sum
            window_counts[window] += count
            local += count
    return (
        n,
        total_sum,
        float(total_sum) / float(n) if n else None,
        tuple(float(s) / float(k) for s, k in zip(window_sums, window_counts)),
    )


def _dc_samples(pcm: bytes, mu: float) -> tuple[object, int]:
    """Deterministic float64 subtraction/normalization, clip, then float32."""
    import numpy as np

    # Use a single float64 temporary to avoid producing a wrapped PCM16 result.
    pcm_i16 = np.frombuffer(pcm, dtype="<i2")
    working = pcm_i16.astype(np.float64)
    working -= np.float64(mu)
    working /= np.float64(32768.0)
    clipped = int(np.count_nonzero((working < -1.0) | (working > 1.0)))
    np.clip(working, -1.0, 1.0, out=working)
    return working.astype(np.float32), clipped


def _digest_ranges(ranges: Sequence[tuple[int, int]]) -> str:
    # This digest does not disclose personally attributable speech boundaries.
    return hashlib.sha256(canonical_json(ranges).encode("utf-8")).hexdigest()


def _variant(name: str, raw_ranges: list[dict], total: int,
             annotation: dict | None) -> DiagnosticVariant:
    ranges = tuple(normalize_ranges(raw_ranges, total))
    metrics = None
    if annotation is not None:
        metrics = score_fixture(
            annotation["fixture_id"], annotation["category"],
            total_samples=total, reference=annotation["reference"],
            predicted=ranges,
        )
    return DiagnosticVariant(
        name=name,
        ranges=ranges,
        ranges_sha256=_digest_ranges(ranges),
        detected_samples=sum(b - a for a, b in ranges),
        metrics=metrics,
    )


def read_annotation(path: str | Path) -> tuple[dict, str]:
    """Load private, locally-held reference intervals; never publish its text."""
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1_000_000:
        raise VadError("invalid diagnostic annotation")
    try:
        raw = source.read_bytes()
        obj = json.loads(raw)
        if (not isinstance(obj, dict)
                or not isinstance(obj.get("fixture_id"), str)
                or not isinstance(obj.get("category"), str)
                or len(obj["fixture_id"]) > 128 or len(obj["category"]) > 128
                or type(obj.get("total_samples")) is not int
                or not isinstance(obj.get("source_sha256"), str)
                or not _SHA.fullmatch(obj["source_sha256"])
                or not isinstance(obj.get("reference"), list)):
            raise ValueError("invalid fields")
        reference = obj["reference"]
        if len(reference) > 100_000:
            raise ValueError("too many ranges")
        for item in reference:
            if (not isinstance(item, list) or len(item) != 2
                    or any(type(x) is not int for x in item)):
                raise ValueError("invalid sample range")
        return ({
            "fixture_id": obj["fixture_id"],
            "category": obj["category"],
            "total_samples": obj["total_samples"],
            "source_sha256": obj["source_sha256"],
            "reference": tuple(tuple(x) for x in reference),
        }, hashlib.sha256(raw).hexdigest())
    except (ValueError, TypeError, UnicodeDecodeError, OSError, json.JSONDecodeError):
        raise VadError("invalid diagnostic annotation") from None


def _lock_sha256() -> str | None:
    # Editable installation: pc/src/m5daylog/ -> pc/uv.lock.
    lock = Path(__file__).resolve().parents[2] / "uv.lock"
    if not lock.is_file() or lock.is_symlink():
        return None
    h = hashlib.sha256()
    with lock.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _revalidate_source(db: sqlite3.Connection, rid: str, row: sqlite3.Row,
                       root: Path, resolved: Path, source_sha: str,
                       source_bytes: int) -> None:
    new = db.execute(
        "SELECT recording_id, started_at, local_path, sync_status, "
        "source_sha256, size_bytes, duration_ms FROM recordings WHERE recording_id=?",
        (rid,),
    ).fetchone()
    if (new is None or any(new[k] != row[k] for k in
            ("started_at", "local_path", "sync_status", "source_sha256",
             "size_bytes", "duration_ms"))):
        raise VadError("diagnostic recording changed")
    path = Path(new["local_path"])
    if (path.resolve(strict=True) != resolved or
            not resolved.is_relative_to(root) or not resolved.is_file()):
        raise VadError("diagnostic source path changed")
    digest = hashlib.sha256()
    count = 0
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
            count += len(block)
    if count != source_bytes or digest.hexdigest() != source_sha:
        raise VadError("diagnostic source changed")


def run_diagnostic(recording_id: str, *, db_path: str | Path,
                   library_root: str | Path | None = None,
                   annotation: dict | None = None,
                   annotation_sha256: str | None = None,
                   code_revision: str | None = None,
                   runtime: object | None = None,
                   max_samples: int = MAX_DIAGNOSTIC_SAMPLES) -> DiagnosticResult:
    """Explicit, offline-only two-variant inference with zero canonical writes."""
    if not isinstance(recording_id, str) or not _UUID.fullmatch(recording_id):
        raise VadError("invalid recording identity")
    if type(max_samples) is not int or max_samples < 0:
        raise VadError("invalid diagnostic resource limit")
    root = resolve_library_root(library_root).resolve(strict=True)
    uri = Path(db_path).resolve(strict=True).as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=0.1) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        row = db.execute("SELECT * FROM recordings WHERE recording_id=?",
                         (recording_id,)).fetchone()
        if row is None:
            raise VadError("unknown recording")
        # Check untrusted DB size before _source reads the full WAV.
        if (type(row["size_bytes"]) is not int or row["size_bytes"] < 0 or
                row["size_bytes"] > 2 * max_samples + MAX_WAV_HEADER_BYTES):
            raise DiagnosticResourceBlocked("DIAGNOSTIC_RESOURCE_BLOCKED")
        # Check physical file size before _source's all-at-once read.
        candidate = Path(row["local_path"] or "")
        if candidate.stat().st_size > 2 * max_samples + MAX_WAV_HEADER_BYTES:
            raise DiagnosticResourceBlocked("DIAGNOSTIC_RESOURCE_BLOCKED")
        pcm, total, source_sha, resolved = _source(row, root)
        if total > max_samples:
            raise DiagnosticResourceBlocked("DIAGNOSTIC_RESOURCE_BLOCKED")
        if annotation is not None:
            if (type(annotation.get("total_samples")) is not int or
                    annotation["total_samples"] != total or
                    annotation.get("source_sha256") != source_sha):
                raise VadError("diagnostic annotation source identity mismatch")
        n, sample_sum, mu, drift = _stat(pcm)
        if n != total:
            raise VadError("diagnostic sample count mismatch")

        model_runtime = runtime if runtime is not None else SileroCPU()
        info = metadata(model_runtime)
        if (info["package_version"] != "6.2.3" or
                info["runtime_version"] != "1.30.0" or
                not _SHA.fullmatch(info["model_sha256"])):
            raise VadError("invalid diagnostic runtime")
        version = fingerprint(info)
        try:
            if total:
                raw_ranges = model_runtime.detect(pcm)
                corrected, clipped = _dc_samples(pcm, mu)
                dc_ranges = model_runtime.detect_float32(corrected)
            else:
                raw_ranges, dc_ranges, clipped = [], [], 0
        except MemoryError:
            raise DiagnosticResourceBlocked("DIAGNOSTIC_RESOURCE_BLOCKED") from None
        raw_variant = _variant(RAW, raw_ranges, total, annotation)
        dc_variant = _variant(DC, dc_ranges, total, annotation)
        _revalidate_source(db, recording_id, row, root, resolved,
                           source_sha, row["size_bytes"])
        return DiagnosticResult(
            source_sha256=source_sha, total_samples=total,
            sample_sum=sample_sum, mean_pcm_lsb=mu,
            clipped_samples=clipped, drift_window_means=drift,
            producer_version=version, model_sha256=info["model_sha256"],
            lock_sha256=_lock_sha256(), code_revision=code_revision,
            annotation_sha256=annotation_sha256, raw=raw_variant, dc=dc_variant,
        )


def aggregate_annotated(results: Sequence[DiagnosticResult]) -> dict:
    """Pooled variant metrics from independently annotated local recordings.

    Numerical threshold flags from vad_eval are NOT Human Gate acceptance.
    Neither a longer detection duration nor a threshold alone proves the
    absence of systematic missed utterances or excessive false positives.
    """
    if not results or any(r.annotation_sha256 is None
                          or r.raw.metrics is None or r.dc.metrics is None
                          for r in results):
        raise VadError("pooled diagnostic requires fully annotated results")
    keys = [(r.source_sha256, r.total_samples) for r in results]
    if len(keys) != len(set(keys)):
        raise VadError("duplicate diagnostic source in pool")
    variants = {}
    for name in (RAW, DC):
        metrics = [r.raw.metrics if name == RAW else r.dc.metrics for r in results]
        pooled = aggregate_speech(metrics)
        false_pos = sum(m.false_positive_samples for m in metrics)
        nonspeech = sum(r.total_samples - m.reference_samples
                        for r, m in zip(results, metrics))
        variants[name] = {
            **pooled,
            "false_positive_samples": false_pos,
            "false_positive_seconds": false_pos / SAMPLE_RATE,
            "false_positive_ratio_over_nonspeech": (
                false_pos / nonspeech if nonspeech else None
            ),
            "fixtures": len(metrics),
        }
    return {
        "status": "MEASUREMENTS_ONLY_MANUAL_EV018_REVIEW_REQUIRED",
        "variants": variants,
    }
