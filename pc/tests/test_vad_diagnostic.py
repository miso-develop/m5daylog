"""Synthetic-only, read-only DC-bias diagnostic tests (Issue #54 section 16)."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    import numpy as np
except ImportError:
    np = None

from m5daylog.db import init_db
from m5daylog.vad import VadError
from m5daylog.vad_diagnostic import (
    BLOCK_SAMPLES, DC, RAW, DRIFT_WINDOW_SAMPLES,
    DiagnosticResourceBlocked, _dc_samples, _stat, aggregate_annotated, read_annotation,
    run_diagnostic,
)

RID = "12345678-1234-4123-8123-123456789012"
STARTED = "2026-10-04T08:00:00+09:00"


def make_wav(samples: list[int], *, channels=1, rate=16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return out.getvalue()


class StubRuntime:
    silero_version = "6.2.3"
    torch_version = "2.9.0"
    onnxruntime_version = "1.30.0"
    model_sha256 = "a" * 64

    def __init__(self):
        self.raw = None
        self.dc = None
        self.mutate = None

    def detect(self, samples: bytes):
        self.raw = bytes(samples)
        if self.mutate is not None:
            self.mutate()
        return [{"start": 1, "end": 4}] if len(samples) >= 8 else []

    def detect_float32(self, audio):
        self.dc = audio.copy()
        return [{"start": 0, "end": 4}] if len(audio) >= 4 else []


@unittest.skipIf(np is None, "numpy not installed in the dependency-free smoke phase")
class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        self.raw_path = self.root / "2026/10/04/raw" / (RID + ".wav")
        self.raw_path.parent.mkdir(parents=True)
        self.db_path = Path(self.tmp.name) / "state" / "m5daylog.db"
        init_db(self.db_path)
        self.db = sqlite3.connect(self.db_path)
        self.addCleanup(self.db.close)
        self.runtime = StubRuntime()
        self.set_audio([1200, 1200, 2200, 1200, 1400])

    def set_audio(self, samples: list[int], **kwargs):
        payload = make_wav(samples, **kwargs)
        self.raw_path.write_bytes(payload)
        self.db.execute(
            """INSERT INTO recordings
               (recording_id,started_at,source_sha256,size_bytes,
                local_path,sync_status)
               VALUES (?,?,?,?,?,'SYNCED')
               ON CONFLICT(recording_id) DO UPDATE SET
                source_sha256=excluded.source_sha256,
                size_bytes=excluded.size_bytes,
                local_path=excluded.local_path""",
            (RID, STARTED, hashlib.sha256(payload).hexdigest(),
             len(payload), str(self.raw_path)),
        )
        self.db.commit()
        return payload

    def run_diag(self, **kw):
        return run_diagnostic(
            RID, db_path=self.db_path, library_root=self.root,
            runtime=self.runtime, **kw,
        )

    def test_raw_and_dc_same_source_original_timeline_and_no_db_writes(self):
        before_pcm = self.raw_path.read_bytes()
        before_db = self.db_path.read_bytes()
        result = self.run_diag()
        self.assertEqual(result.total_samples, 5)
        self.assertEqual(result.sample_sum, 7200)
        self.assertEqual(result.mean_pcm_lsb, 1440)
        self.assertEqual(result.clipped_samples, 0)
        self.assertEqual(result.raw.name, RAW)
        self.assertEqual(result.dc.name, DC)
        self.assertEqual(result.raw.ranges, ((1, 4),))
        self.assertEqual(result.dc.ranges, ((0, 4),))
        np.testing.assert_array_equal(
            self.runtime.dc,
            np.clip((np.array([1200, 1200, 2200, 1200, 1400],
                              dtype=np.float64) - 1440) / 32768.0,
                    -1.0, 1.0).astype(np.float32),
        )
        self.assertEqual(self.raw_path.read_bytes(), before_pcm)
        self.assertEqual(self.db_path.read_bytes(), before_db)
        self.assertFalse((self.raw_path.parent.parent / "vad").exists())
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(result.summary()["status"],
                         "DIAGNOSTIC_ONLY_NOT_EV018_ACCEPTED")

    def test_zero_frames_does_not_call_model(self):
        self.set_audio([])
        result = self.run_diag()
        self.assertIsNone(result.mean_pcm_lsb)
        self.assertEqual(result.sample_sum, 0)
        self.assertEqual(result.raw.ranges, ())
        self.assertEqual(result.dc.ranges, ())
        self.assertIsNone(self.runtime.raw)
        self.assertIsNone(self.runtime.dc)

    def test_negative_bias_and_low_level_speech_retained(self):
        self.set_audio([-1500, -1510, -1490, -1500, -1100])
        result = self.run_diag()
        self.assertEqual(result.sample_sum, -7100)
        self.assertEqual(result.mean_pcm_lsb, -1420)
        self.assertAlmostEqual(float(self.runtime.dc[4]), 320 / 32768.0)

    def test_extrema_clipped_without_wrapping(self):
        samples = struct.pack("<hhh", -32768, -32768, 32767)
        signal, clipped = _dc_samples(samples, -32769 / 3)
        self.assertEqual(clipped, 1)
        self.assertAlmostEqual(float(signal[2]), 1.0)
        self.assertTrue(np.all(signal <= 1.0))
        self.assertTrue(np.all(signal >= -1.0))

    def test_cross_block_exact_sums(self):
        pcm = struct.pack("<h", -32768) * (BLOCK_SAMPLES + 3)
        n, total, mu, drift = _stat(pcm)
        self.assertEqual(n, BLOCK_SAMPLES + 3)
        self.assertEqual(total, -32768 * n)
        self.assertEqual(mu, -32768)
        self.assertEqual(drift, (-32768.0,))

    def test_drift_window_cross_boundary_and_partial_tail(self):
        pcm = (struct.pack("<h", 100) * DRIFT_WINDOW_SAMPLES
               + struct.pack("<h", 200) * 3)
        n, total, mu, drift = _stat(pcm)
        self.assertEqual(n, DRIFT_WINDOW_SAMPLES + 3)
        self.assertEqual(total, 100 * DRIFT_WINDOW_SAMPLES + 600)
        self.assertEqual(drift, (100.0, 200.0))
        self.assertAlmostEqual(mu, total / n)

    def test_resource_limit_has_no_product_side_effects(self):
        before = self.db_path.read_bytes()
        with self.assertRaises(DiagnosticResourceBlocked):
            self.run_diag(max_samples=2)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertIsNone(self.runtime.raw)

    def test_source_hash_and_format_rejection(self):
        original = self.raw_path.read_bytes()
        self.raw_path.write_bytes(original + b"tampered")
        with self.assertRaises(VadError):
            self.run_diag()
        self.set_audio([1, 2, 3, 4], rate=8000)
        with self.assertRaisesRegex(VadError, "WAV"):
            self.run_diag()
        self.set_audio([1, 2, 3, 4])
        foreign = Path(self.tmp.name) / "foreign.wav"
        foreign.write_bytes(self.raw_path.read_bytes())
        self.db.execute("UPDATE recordings SET local_path=? WHERE recording_id=?",
                        (str(foreign), RID))
        self.db.commit()
        with self.assertRaises(VadError):
            self.run_diag()

    def test_toctou_change_after_inference_cannot_be_accepted(self):
        expected = self.raw_path.read_bytes()
        self.runtime.mutate = lambda: self.raw_path.write_bytes(expected + b"X")
        with self.assertRaisesRegex(VadError, "source changed"):
            self.run_diag()
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_annotation_must_match_hash_and_produces_both_variant_scores(self):
        annotation = {
            "fixture_id": "synthetic", "category": "quiet",
            "source_sha256": hashlib.sha256(self.raw_path.read_bytes()).hexdigest(),
            "total_samples": 5, "reference": [[0, 4]],
        }
        ref_file = Path(self.tmp.name) / "reference.json"
        ref_file.write_text(json.dumps(annotation), encoding="utf-8")
        parsed, digest = read_annotation(ref_file)
        result = self.run_diag(annotation=parsed, annotation_sha256=digest)
        self.assertEqual(result.annotation_sha256, digest)
        self.assertEqual(result.raw.metrics.recovered_samples, 3)
        self.assertEqual(result.dc.metrics.recovered_samples, 4)
        self.assertEqual(result.raw.metrics.false_positive_samples, 0)
        self.assertEqual(result.raw.metrics.recall, 0.75)
        self.assertEqual(result.dc.metrics.recall, 1.0)
        self.assertNotEqual(result.raw.ranges_sha256, result.dc.ranges_sha256)
        annotation["source_sha256"] = "0" * 64
        ref_file.write_text(json.dumps(annotation), encoding="utf-8")
        with self.assertRaisesRegex(VadError, "identity"):
            self.run_diag(annotation=read_annotation(ref_file)[0])

    def test_pooled_pair_rejects_unannotated_and_duplicate_sources(self):
        with self.assertRaises(VadError):
            aggregate_annotated([self.run_diag()])
        annotation = {
            "fixture_id": "synthetic", "category": "quiet",
            "source_sha256": hashlib.sha256(self.raw_path.read_bytes()).hexdigest(),
            "total_samples": 5, "reference": ((0, 4),),
        }
        result = self.run_diag(annotation=annotation, annotation_sha256="f" * 64)
        pooled = aggregate_annotated([result])
        self.assertAlmostEqual(pooled["variants"][RAW]["speech_recall"], 0.75)
        self.assertAlmostEqual(pooled["variants"][DC]["speech_recall"], 1.0)
        self.assertEqual(pooled["status"],
                         "MEASUREMENTS_ONLY_MANUAL_EV018_REVIEW_REQUIRED")
        with self.assertRaisesRegex(VadError, "duplicate"):
            aggregate_annotated([result, result])

    def test_annotation_rejects_bad_reference_structure(self):
        file = Path(self.tmp.name) / "reference.json"
        file.write_text(json.dumps({"fixture_id": "x", "category": "quiet",
                                    "source_sha256": "f" * 64,
                                    "total_samples": 5, "reference": [[True, 3]]}))
        with self.assertRaises(VadError):
            read_annotation(file)

    def test_sanitized_cli_output_does_not_disclose_audio(self):
        from m5daylog.cli import cmd_vad_diagnose
        from unittest.mock import patch
        from io import StringIO
        out = StringIO()
        with patch("m5daylog.cli.run_diagnostic", return_value=self.run_diag()):
            with patch("sys.stdout", out):
                exit_code = cmd_vad_diagnose(RID, str(self.root),
                                             str(self.db_path), None, None, None)
        self.assertEqual(exit_code, 0)
        printed = out.getvalue()
        self.assertIn("DIAGNOSTIC_ONLY_NOT_EV018_ACCEPTED", printed)
        self.assertNotIn(str(self.raw_path), printed)
        self.assertNotIn(RID, printed)


if __name__ == "__main__":
    unittest.main()
