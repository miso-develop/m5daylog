"""Synthetic-only contract and recovery tests for recording-scoped VAD (#54)."""
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

from m5daylog.db import init_db
from m5daylog.vad import (
    VadError, canonical_json, fingerprint, metadata, normalize_ranges,
    run_vad, validate_jsonl,
)

RID = "12345678-1234-4123-8123-123456789012"
STARTED = "2026-10-04T08:00:00+09:00"


class StubRuntime:
    silero_version = "6.2.3"
    torch_version = "2.9.0"
    onnxruntime_version = "1.30.0"
    model_sha256 = "a" * 64

    def __init__(self, output=None):
        self.output = output if output is not None else []
        self.calls = 0

    def detect(self, samples):
        self.calls += 1
        return self.output


def wav_bytes(frames=16000, channels=1, rate=16000, width=2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as w:
        w.setnchannels(channels)
        w.setframerate(rate)
        w.setsampwidth(width)
        w.writeframes(b"\0" * frames * channels * width)
    return stream.getvalue()


class VadCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        self.db_path = Path(self.tmp.name) / "state" / "m5daylog.db"
        init_db(self.db_path)
        self.raw = self.root / "2026/10/04/raw" / (RID + ".wav")
        self.raw.parent.mkdir(parents=True)
        self.raw.write_bytes(wav_bytes())
        self.original = self.raw.read_bytes()
        self.con = sqlite3.connect(self.db_path)
        self.addCleanup(self.con.close)
        self.con.execute(
            """INSERT INTO recordings
               (recording_id,started_at,source_sha256,size_bytes,local_path,sync_status)
               VALUES (?,?,?,?,?,'SYNCED')""",
            (RID, STARTED, hashlib.sha256(self.original).hexdigest(),
             len(self.original), str(self.raw)),
        )
        self.con.commit()
        self.runtime = StubRuntime([{"start": 50, "end": 200},
                                    {"start": 200, "end": 350},
                                    {"start": 1000, "end": 1600}])

    def execute(self, runtime=None):
        return run_vad(RID, db_path=self.db_path, library_root=self.root,
                       runtime=runtime or self.runtime)

    def outfile(self):
        return self.root / "2026/10/04/vad" / (RID + ".jsonl")

    def test_success_schema_timeline_sqlite_and_no_raw_mutation(self):
        result = self.execute()
        self.assertEqual((result.status, result.ranges), ("SUCCEEDED", 2))
        self.assertEqual(self.runtime.calls, 1)
        lines = [json.loads(x) for x in self.outfile().read_text().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1]["start_sample"], 50)
        self.assertEqual(lines[1]["end_sample"], 350)
        self.assertEqual(lines[1]["start_sec"], 50 / 16000)
        self.assertEqual(lines[2]["start_sample"], 1000)
        self.assertEqual(lines[0]["started_at"], STARTED)
        self.assertEqual(lines[0]["producer"], "m5daylog.vad")
        self.assertEqual(self.raw.read_bytes(), self.original)
        self.assertEqual(
            self.con.execute("SELECT vad_status FROM recordings").fetchone()[0],
            "SUCCEEDED")
        self.assertEqual(
            self.con.execute("SELECT job_type,status,attempts FROM jobs").fetchone(),
            ("VAD", "SUCCEEDED", 1))
        self.assertEqual(
            self.con.execute("SELECT artifact_type,sha256 FROM artifacts").fetchone(),
            ("vad", result.sha256))

    def test_skip_does_not_call_model_and_does_not_increment_attempts(self):
        initial = self.execute()
        self.runtime.output = [{"start": 120, "end": 300}]
        second = self.execute()
        self.assertEqual(second.status, "SKIPPED")
        self.assertEqual(second.sha256, initial.sha256)
        self.assertEqual(self.runtime.calls, 1)
        self.assertEqual(
            self.con.execute("SELECT attempts FROM jobs").fetchone()[0], 1)

    def test_crash_after_publication_before_sqlite_commit_adopts_final(self):
        before = self.execute()
        self.con.execute("DELETE FROM artifacts")
        self.con.execute("UPDATE recordings SET vad_status='RECOVERABLE'")
        self.con.commit()
        later = self.execute()
        self.assertEqual(later.status, "RECOVERED")
        self.assertEqual(later.sha256, before.sha256)
        self.assertEqual(self.runtime.calls, 1)

    def test_zero_speech_is_valid_meta_only_artifact(self):
        self.runtime.output = []
        result = self.execute()
        self.assertEqual(result.ranges, 0)
        self.assertEqual(len(self.outfile().read_text().splitlines()), 1)
        self.assertEqual(result.status, "SUCCEEDED")

    def test_canonical_but_tampered_metadata_never_skips_inference(self):
        first = self.execute()
        records = [json.loads(line) for line in self.outfile().read_text().splitlines()]
        records[0]["params"]["threshold"] = 0.99
        forged = ("\n".join(canonical_json(item) for item in records) + "\n").encode("utf-8")
        self.outfile().write_bytes(forged)
        self.con.execute("UPDATE artifacts SET sha256=?",
                         (hashlib.sha256(forged).hexdigest(),))
        self.con.commit()
        second = self.execute()
        self.assertEqual(second.status, "SUCCEEDED")
        self.assertEqual(self.runtime.calls, 2)
        self.assertEqual(second.sha256, first.sha256)

    def test_model_behavior_identity_changes_recompute(self):
        first = self.execute()
        other = StubRuntime([{"start": 2, "end": 100}])
        other.model_sha256 = "b" * 64
        second = self.execute(other)
        self.assertEqual(second.status, "SUCCEEDED")
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(other.calls, 1)

    def test_corrupted_artifact_does_not_skip(self):
        self.execute()
        self.outfile().write_text('{"type":"meta"}\n')
        result = self.execute()
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(self.runtime.calls, 2)

    def test_concurrent_sync_conflict_never_commits_vad_success(self):
        original_detect = self.runtime.detect

        def conflicting_sync(samples):
            with sqlite3.connect(self.db_path) as other:
                other.execute(
                    "UPDATE recordings SET sync_status='CONFLICT' WHERE recording_id=?",
                    (RID,),
                )
            return original_detect(samples)

        self.runtime.detect = conflicting_sync
        with self.assertRaisesRegex(VadError, "recording changed during VAD"):
            self.execute()
        self.assertEqual(self.con.execute(
            "SELECT sync_status, vad_status FROM recordings").fetchone(),
            ("CONFLICT", "FAILED"))
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(self.con.execute(
            "SELECT status FROM jobs").fetchone()[0], "FAILED")
        self.assertEqual(self.raw.read_bytes(), self.original)

    def test_same_day_timestamp_change_invalidates_cached_artifact(self):
        first = self.execute()
        changed = "2026-10-04T08:00:05+09:00"
        self.con.execute("UPDATE recordings SET started_at=?", (changed,))
        self.con.commit()
        result = self.execute()
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertNotEqual(result.sha256, first.sha256)
        self.assertEqual(self.runtime.calls, 2)
        meta = json.loads(self.outfile().read_text().splitlines()[0])
        self.assertEqual(meta["started_at"], changed)

    def test_corrupt_cached_timestamp_cannot_be_adopted(self):
        self.execute()
        lines = [json.loads(line) for line in self.outfile().read_text().splitlines()]
        lines[0]["started_at"] = "not-a-timestamp"
        forged = ("\n".join(canonical_json(item) for item in lines) + "\n").encode()
        self.outfile().write_bytes(forged)
        self.con.execute("UPDATE artifacts SET sha256=?",
                         (hashlib.sha256(forged).hexdigest(),))
        self.con.commit()
        self.assertEqual(self.execute().status, "SUCCEEDED")
        self.assertEqual(self.runtime.calls, 2)

    def test_partial_pcm16_data_chunk_is_rejected(self):
        raw = bytearray(wav_bytes(frames=1))
        raw[40:44] = (3).to_bytes(4, "little")
        raw.extend(b"\x00\x00")  # third half-sample, plus RIFF padding
        raw[4:8] = (len(raw) - 8).to_bytes(4, "little")
        self.raw.write_bytes(raw)
        self.con.execute(
            "UPDATE recordings SET size_bytes=?,source_sha256=?",
            (len(raw), hashlib.sha256(raw).hexdigest()))
        self.con.commit()
        with wave.open(io.BytesIO(raw), "rb") as wav:
            self.assertEqual(wav.getnframes(), 1)
            self.assertEqual(len(wav.readframes(1)), 2)
        with self.assertRaisesRegex(VadError, "truncated WAV frames"):
            self.execute()
        self.assertEqual(self.runtime.calls, 0)

    def test_input_hash_mismatch_is_fail_closed(self):
        self.raw.write_bytes(self.original + b"X")
        with self.assertRaisesRegex(VadError, "integrity"):
            self.execute()
        self.assertEqual(self.runtime.calls, 0)
        self.assertFalse(self.outfile().exists())

    def test_changed_raw_invalidates_prior_success_before_stt(self):
        self.execute()
        self.raw.write_bytes(self.original + b"modified")
        with self.assertRaisesRegex(VadError, "integrity"):
            self.execute()
        self.assertEqual(
            self.con.execute("SELECT vad_status FROM recordings").fetchone()[0],
            "FAILED")
        self.assertEqual(
            self.con.execute("SELECT status FROM jobs").fetchone()[0],
            "FAILED")
        self.assertEqual(self.runtime.calls, 1)

    def test_invalid_wav_formats(self):
        for payload in (wav_bytes(channels=2), wav_bytes(rate=8000),
                        wav_bytes(width=1), b"not a WAV"):
            with self.subTest(payload=payload[:5]):
                self.raw.write_bytes(payload)
                self.con.execute("UPDATE recordings SET source_sha256=?,size_bytes=?",
                                 (hashlib.sha256(payload).hexdigest(), len(payload)))
                self.con.commit()
                with self.assertRaises(VadError):
                    self.execute()
                self.assertFalse(self.outfile().exists())

    def test_truncated_wav_fails(self):
        payload = self.original[:48]  # declares many frames, physically truncated
        self.raw.write_bytes(payload)
        self.con.execute("UPDATE recordings SET source_sha256=?,size_bytes=?",
                         (hashlib.sha256(payload).hexdigest(), len(payload)))
        self.con.commit()
        with self.assertRaises(VadError):
            self.execute()

    def test_manifest_duration_mismatch_is_rejected(self):
        self.con.execute("UPDATE recordings SET duration_ms=10000")
        self.con.commit()
        with self.assertRaisesRegex(VadError, "duration mismatch"):
            self.execute()
        self.assertEqual(self.runtime.calls, 0)

    def test_manifest_duration_one_ms_rounding_is_accepted(self):
        self.con.execute("UPDATE recordings SET duration_ms=1001")
        self.con.commit()
        self.assertEqual(self.execute().status, "SUCCEEDED")

    def test_non_synced_rejected(self):
        self.con.execute("UPDATE recordings SET sync_status='CONFLICT'")
        self.con.commit()
        with self.assertRaises(VadError):
            self.execute()
        self.assertEqual(self.runtime.calls, 0)

    def test_unsafe_raw_outside_library_rejected(self):
        external = Path(self.tmp.name) / "external.wav"
        external.write_bytes(self.original)
        self.con.execute("UPDATE recordings SET local_path=?", (str(external),))
        self.con.commit()
        with self.assertRaises(VadError):
            self.execute()
        self.assertEqual(external.read_bytes(), self.original)

    def test_temp_symlink_never_truncates_protected_target(self):
        self.outfile().parent.mkdir(parents=True)
        protected = self.root / "important.dat"
        protected.write_bytes(b"must not change")
        temp = Path(str(self.outfile()) + ".tmp")
        try:
            temp.symlink_to(protected)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.execute()
        self.assertEqual(protected.read_bytes(), b"must not change")
        self.assertFalse(temp.exists())

    def test_invalid_model_output_sets_failure_status(self):
        self.runtime.output = [{"start": 400, "end": 0}]
        with self.assertRaises(VadError):
            self.execute()
        self.assertEqual(
            self.con.execute("SELECT vad_status FROM recordings").fetchone()[0],
            "FAILED")
        self.assertFalse(self.outfile().exists())


class PureContractTests(unittest.TestCase):
    def test_merge_touch_and_overlap_only(self):
        ranges = [{"start": 10, "end": 20}, {"start": 22, "end": 30},
                  {"start": -50, "end": 10}, {"start": 25, "end": 40}]
        self.assertEqual(normalize_ranges(ranges, 100), [(0, 20), (22, 40)])

    def test_reject_bool_fraction_and_invalid_end(self):
        for raw in ([{"start": True, "end": 7}],
                    [{"start": 0.5, "end": 7}],
                    [{"start": 2, "end": 2}]):
            with self.assertRaises(VadError):
                normalize_ranges(raw, 100)

    def test_nonfinite_not_in_behavior_fingerprint(self):
        meta = metadata(StubRuntime())
        text = fingerprint(meta)
        self.assertTrue(text.startswith("v1:"))
        self.assertEqual(text, fingerprint(meta))
        self.assertNotIn("Infinity", canonical_json({"model": meta}))

    def test_schema_v1_rejects_forged_canonical_producer_metadata(self):
        from m5daylog.vad import _artifact_bytes
        model = metadata(StubRuntime())
        version = fingerprint(model)
        source_sha = "0" * 64
        generated = _artifact_bytes(
            {"recording_id": RID, "started_at": STARTED},
            model, version, source_sha, 10, [(1, 8)],
        )
        self.assertEqual(validate_jsonl(generated, RID, source_sha, version, 10), 1)
        base = [json.loads(line) for line in generated.splitlines()]
        for field, key, replacement in (
                ("model", "torch_version", "999.0"),
                ("params", "min_silence_ms", 0),
                ("params", "unexpected_parameter", True)):
            with self.subTest(field=field, key=key):
                forged = [dict(item) for item in base]
                forged[0][field] = dict(forged[0][field])
                forged[0][field][key] = replacement
                serialized = ("\n".join(canonical_json(item) for item in forged)
                              + "\n").encode("utf-8")
                with self.assertRaises(VadError):
                    validate_jsonl(serialized, RID, source_sha, version, 10)

    def test_corrupt_jsonl_rejected(self):
        with self.assertRaises(VadError):
            validate_jsonl(b"not-json\n", RID, "0" * 64, "v1:test")


if __name__ == "__main__":
    unittest.main()
