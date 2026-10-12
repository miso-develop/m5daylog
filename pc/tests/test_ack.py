"""Task #52 PC ACK pre-release tests (synthetic metadata only)."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from m5daylog import ack, db

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
DEVICE = "11111111-1111-4111-8111-111111111111"
OTHER_DEVICE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RID = "22222222-2222-4222-8222-222222222222"
OLD_RID = "33333333-3333-4333-8333-333333333333"
SHA = "a" * 64


class Lease(AbstractContextManager):
    def __init__(self, *, reject=False, fail_close=False):
        self.reject = reject
        self.fail_close = fail_close
        self.opened = False
        self.closed = False

    def __enter__(self):
        if self.reject:
            raise ack.AckAdmissionDenied("sealed")
        self.opened = True
        return self

    def __exit__(self, *args):
        self.closed = True
        if self.fail_close:
            raise OSError("synthetic lease close failure")
        return False


class Port:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.events = []

    def replace_atomic(self, temp, final):
        self.events.append("replace")
        if self.failure == "replace":
            raise OSError("synthetic replacement rejection")
        os.replace(temp, final)

    def flush_final(self, final):
        self.events.append("flush")
        if self.failure == "flush":
            raise OSError("synthetic host flush rejection")


class AckTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.mount = root / "device"
        self.db = root / "local.db"
        self.m5root = self.mount / "M5DAYLOG"
        self.ackdir = self.m5root / "acks"
        self.ackdir.mkdir(parents=True)
        (self.m5root / "device.json").write_text(json.dumps({
            "schemaVersion": 1, "deviceId": DEVICE, "model": "synthetic-model",
            "firmwareVersion": "0.0.0", "audioCapabilities": {}
        }), encoding="utf-8")
        (self.m5root / "manifest.json").write_text(json.dumps({
            "schemaVersion": 1, "deviceId": DEVICE, "updatedAt": "2026-10-10T00:00:00Z",
            "recordings": []
        }), encoding="utf-8")
        db.init_db(self.db)
        self.barrier = ack.VerifiedSyncBarrier(DEVICE, True, 0)
        self.insert_recording()

    def insert_recording(self, *, rid=RID, statuses=("SUCCEEDED",)*4, sync="SYNCED", device=DEVICE):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO recordings(recording_id,device_id,source_sha256,sync_status,"
                "vad_status,stt_status,diarization_status,day_status) VALUES(?,?,?,?,?,?,?,?)",
                (rid, device, SHA, sync, *statuses),
            )
            conn.commit()
        finally:
            conn.close()

    def flush(self, *, lease=None, port=None, barrier=None):
        return ack.flush_pre_release_acks(
            self.db, self.mount, self.barrier if barrier is None else barrier,
            write_lease=lease or Lease(), atomic_port=port or Port(),
            contracts_dir=CONTRACTS,
        )

    def pending_state(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT recording_id,last_delivered_at FROM ack_outbox").fetchall()
        finally:
            conn.close()

    def test_no_op_before_all_four_stages_durable(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE recordings SET day_status='FAILED'")
        conn.commit()
        conn.close()
        lease = Lease()
        self.assertEqual(self.flush(lease=lease).status, "NO_OP")
        self.assertFalse(lease.opened)
        self.assertFalse((self.ackdir / "processed.json").exists())
        self.assertEqual(self.pending_state(), [])

    def test_success_creates_pending_and_never_clears_it_before_device_proof(self):
        lease, port = Lease(), Port()
        result = self.flush(lease=lease, port=port)
        self.assertEqual((result.status, result.attempted), ("DELIVERED", 1))
        self.assertTrue(lease.closed)
        self.assertEqual(port.events, ["replace", "flush"])
        result_file = json.loads((self.ackdir / "processed.json").read_text())
        self.assertEqual(result_file["recordingIds"], [RID])
        self.assertEqual(result_file["deviceId"], DEVICE)
        self.assertEqual([x[0] for x in self.pending_state()], [RID])
        self.assertIsNotNone(self.pending_state()[0][1])
        self.assertEqual(self.flush().status, "DELIVERED")

    def test_gate_sealed_preserves_retry_and_does_no_device_io(self):
        lease = Lease(reject=True)
        self.assertEqual(self.flush(lease=lease).status, "DEFERRED")
        self.assertFalse(lease.opened)
        self.assertEqual(self.pending_state(), [(RID, None)])
        self.assertFalse((self.ackdir / "processed.json").exists())

    def test_sync_barrier_failure_prevents_outbox_and_device_io(self):
        with self.assertRaises(ack.AckSafetyError):
            self.flush(barrier=ack.VerifiedSyncBarrier(DEVICE, True, 1))
        with self.assertRaises(ack.AckSafetyError):
            self.flush(barrier=ack.VerifiedSyncBarrier(DEVICE, False, 0))
        self.assertFalse((self.ackdir / "processed.json").exists())

    def test_rename_error_preserves_old_final_and_retry(self):
        final = self.ackdir / "processed.json"
        old = {
            "schemaVersion": 1, "deviceId": DEVICE,
            "processedAt": "2026-10-09T00:00:00Z", "recordingIds": [OLD_RID],
        }
        original = json.dumps(old).encode()
        final.write_bytes(original)
        result = self.flush(port=Port(failure="replace"))
        self.assertEqual(result.status, "DEFERRED")
        self.assertEqual(final.read_bytes(), original)
        self.assertEqual(self.pending_state(), [(RID, None)])
        self.assertEqual(self.flush().status, "DELIVERED")
        self.assertEqual(json.loads(final.read_text())["recordingIds"], [RID, OLD_RID])

    def test_host_flush_failure_after_final_publish_is_retryable(self):
        result = self.flush(port=Port(failure="flush"))
        self.assertEqual(result.status, "DEFERRED")
        self.assertEqual(self.pending_state(), [(RID, None)])
        self.assertEqual(self.flush().status, "DELIVERED")

    def test_crash_after_publish_before_local_state_is_duplicate_safe(self):
        with patch.object(ack, "_mark_delivered", side_effect=sqlite3.OperationalError("synthetic")):
            self.assertEqual(self.flush().status, "DEFERRED")
        self.assertEqual(self.pending_state(), [(RID, None)])
        self.assertEqual(self.flush().status, "DELIVERED")
        self.assertEqual(json.loads((self.ackdir / "processed.json").read_text())["recordingIds"], [RID])

    def test_stale_temp_file_is_non_authoritative(self):
        tmp = self.ackdir / "processed.json.tmp"
        tmp.write_text("incomplete")
        self.assertEqual(self.flush().status, "DELIVERED")
        self.assertFalse(tmp.exists())

    def test_wrong_device_and_invalid_old_file_are_fail_closed(self):
        final = self.ackdir / "processed.json"
        final.write_text("broken json")
        before = final.read_bytes()
        self.assertEqual(self.flush().status, "DEFERRED")
        self.assertEqual(final.read_bytes(), before)
        final.write_text(json.dumps({
            "schemaVersion": 1, "deviceId": OTHER_DEVICE,
            "processedAt": "2026-10-09T00:00:00Z", "recordingIds": [OLD_RID],
        }))
        before = final.read_bytes()
        self.assertEqual(self.flush().status, "DEFERRED")
        self.assertEqual(final.read_bytes(), before)

    def test_changed_raw_hash_or_conflict_never_reuses_pending(self):
        self.flush(lease=Lease(reject=True))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE recordings SET source_sha256=?,sync_status='CONFLICT'", ("b" * 64,))
        conn.commit()
        conn.close()
        lease = Lease()
        self.assertEqual(self.flush(lease=lease).status, "NO_OP")
        self.assertFalse(lease.opened)

    def test_close_failure_blocks_release(self):
        with self.assertRaises(ack.AckHandleUnsafeError):
            self.flush(lease=Lease(fail_close=True))

    def test_snapshot_does_not_wait_for_later_processing(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE recordings SET day_status='FAILED'")
        conn.commit()
        conn.close()
        self.insert_recording(rid=OLD_RID)
        self.assertEqual(self.flush().attempted, 1)
        content = json.loads((self.ackdir / "processed.json").read_text())
        self.assertEqual(content["recordingIds"], [OLD_RID])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE recordings SET day_status='SUCCEEDED' WHERE recording_id=?", (RID,))
        conn.commit()
        conn.close()
        self.assertEqual(self.flush().attempted, 2)


if __name__ == "__main__":
    unittest.main()
