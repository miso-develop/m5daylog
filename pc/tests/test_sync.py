"""Task #51 manifest-based incremental sync tests.

All identifiers, metadata, paths, and byte payloads are synthetic.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from m5daylog import cli, db

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts"
FIXTURES = REPO / "fixtures"
DEVICE_ID = "11111111-1111-4111-8111-111111111111"
RECORDING_ID = "22222222-2222-4222-8222-222222222222"


def sync_module():
    from m5daylog import sync
    return sync


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[rel] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[rel] = ("file", path.read_bytes())
        elif path.is_dir():
            result[rel] = ("dir", "")
    return result


def write_device(
    mount: Path,
    payload: bytes = b"synthetic-wav-payload",
    *,
    manifest_device_id: str = DEVICE_ID,
    filename: str = "recordings/source.wav",
    started_at: str = "2026-10-03T01:02:03+09:00",
) -> dict:
    root = mount / "M5DAYLOG"
    recordings = root / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    device = {
        "schemaVersion": 1,
        "deviceId": DEVICE_ID,
        "model": "M5Capsule-SYNTHETIC",
        "firmwareVersion": "0.0.0-test",
        "audioCapabilities": {},
    }
    item = {
        "recordingId": RECORDING_ID,
        "filename": filename,
        "startedAt": started_at,
        "durationMs": 1000,
        "sizeBytes": len(payload),
        "sha256": digest(payload),
        "sampleRate": 16000,
        "bitDepth": 16,
        "channels": 1,
        "state": "finalized",
        "firmwareVersion": "0.0.0-test",
    }
    manifest = {
        "schemaVersion": 1,
        "deviceId": manifest_device_id,
        "updatedAt": "2026-10-03T01:05:00+09:00",
        "recordings": [item],
    }
    (root / "device.json").write_text(json.dumps(device), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if filename.startswith("recordings/") and ".." not in Path(filename).parts:
        source = root.joinpath(*Path(filename).parts)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
    return manifest


def rewrite_manifest(mount: Path, mutate) -> None:
    path = mount / "M5DAYLOG" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def recording_row(db_path: Path):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "SELECT recording_id, device_id, started_at, source_filename, "
            "source_sha256, size_bytes, local_path, sync_status FROM recordings "
            "WHERE recording_id=?",
            (RECORDING_ID,),
        ).fetchone()
    finally:
        con.close()


class InterruptingReader:
    def __init__(self, payload: bytes, fail_after: int):
        self._stream = io.BytesIO(payload)
        self._fail_after = fail_after
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        if self._read >= self._fail_after:
            raise OSError("synthetic disconnect")
        remaining = self._fail_after - self._read
        if size < 0 or size > remaining:
            size = remaining
        chunk = self._stream.read(size)
        self._read += len(chunk)
        return chunk


class ContractTests(unittest.TestCase):
    def test_uses_canonical_device_and_manifest_fixtures(self):
        sync = sync_module()
        for kind in ("device", "manifest"):
            schema = CONTRACTS / f"{kind}.schema.json"
            for path in sorted((FIXTURES / kind / "valid").glob("*.json")):
                sync.validate_contract_document(
                    json.loads(path.read_text(encoding="utf-8")), schema
                )
            for path in sorted((FIXTURES / kind / "invalid").glob("*.json")):
                with self.assertRaises(sync.ContractValidationError):
                    sync.validate_contract_document(
                        json.loads(path.read_text(encoding="utf-8")), schema
                    )


class SafetyTests(unittest.TestCase):
    def assert_rejected_unchanged(self, mount: Path, library: Path, db_path: Path):
        sync = sync_module()
        before = snapshot(mount)
        with self.assertRaises((sync.ContractValidationError, sync.SyncValidationError)):
            sync.sync_device(
                mount,
                library_root=library,
                db_path=db_path,
                contracts_dir=CONTRACTS,
            )
        self.assertEqual(before, snapshot(mount))
        self.assertEqual([], list(library.rglob("*.wav")) if library.exists() else [])

    def test_invalid_device_is_read_only(self):
        sync = sync_module()
        with TemporaryDirectory() as tmp:
            mount = Path(tmp) / "drive"
            write_device(mount)
            path = mount / "M5DAYLOG" / "device.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            del value["model"]
            path.write_text(json.dumps(value), encoding="utf-8")
            before = snapshot(mount)
            with self.assertRaises(sync.ContractValidationError):
                sync.inspect_device(mount, contracts_dir=CONTRACTS)
            self.assertEqual(before, snapshot(mount))

    def test_device_id_mismatch_rejected_before_copy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            write_device(
                mount,
                manifest_device_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
            self.assert_rejected_unchanged(mount, root / "lib", root / "state.db")

    def test_duplicate_recording_id_rejected_before_copy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            manifest = write_device(mount)
            duplicate = dict(manifest["recordings"][0])
            duplicate["filename"] = "recordings/duplicate.wav"
            (mount / "M5DAYLOG" / "recordings" / "duplicate.wav").write_bytes(
                b"synthetic-wav-payload"
            )
            rewrite_manifest(
                mount, lambda doc: doc["recordings"].append(duplicate)
            )
            self.assert_rejected_unchanged(mount, root / "lib", root / "state.db")

    def test_path_escape_variants_rejected_before_copy(self):
        for filename in ("recordings/../outside.wav", "/absolute.wav", r"C:\outside.wav"):
            with self.subTest(filename=filename), TemporaryDirectory() as tmp:
                root = Path(tmp)
                mount = root / "drive"
                write_device(mount, filename=filename)
                self.assert_rejected_unchanged(
                    mount, root / "lib", root / "state.db"
                )

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink unsupported")
    def test_symlink_escape_rejected_before_copy(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "drive"
            outside = root / "outside"
            outside.mkdir()
            (outside / "outside.wav").write_bytes(b"synthetic-wav-payload")
            write_device(mount, filename="recordings/link/outside.wav")
            link = mount / "M5DAYLOG" / "recordings" / "link"
            (link / "outside.wav").unlink()
            link.rmdir()
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(type(exc).__name__)
            self.assert_rejected_unchanged(mount, root / "lib", root / "state.db")


class SyncTests(unittest.TestCase):
    def run_sync(self, mount, library, db_path):
        return sync_module().sync_device(
            mount,
            library_root=library,
            db_path=db_path,
            contracts_dir=CONTRACTS,
        )

    def test_sync_path_db_and_device_read_only(self):
        payload = b"synthetic-recording-001"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(
                mount,
                payload,
                started_at="2026-10-03T23:30:00-05:00",
                filename="recordings/source-name.wav",
            )
            before = snapshot(mount)
            summary = self.run_sync(mount, library, db_path)
            final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
            self.assertEqual((summary.copied, summary.conflicts), (1, 0))
            self.assertEqual(final.read_bytes(), payload)
            self.assertEqual(before, snapshot(mount))
            row = recording_row(db_path)
            self.assertEqual(row[0], RECORDING_ID)
            self.assertEqual(row[2], "2026-10-03T23:30:00-05:00")
            self.assertEqual(row[3], "recordings/source-name.wav")
            self.assertEqual(Path(row[6]), final)
            self.assertEqual(row[7], "SYNCED")

    def test_second_sync_copies_zero_and_has_one_db_row(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount)
            first = self.run_sync(mount, library, db_path)
            second = self.run_sync(mount, library, db_path)
            self.assertEqual(first.copied, 1)
            self.assertEqual((second.copied, second.reused), (0, 1))
            con = sqlite3.connect(str(db_path))
            try:
                count = con.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(count, 1)

    def test_existing_final_matching_converges_without_copy(self):
        payload = b"synthetic-existing-final"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount, payload)
            final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
            final.parent.mkdir(parents=True)
            final.write_bytes(payload)
            summary = self.run_sync(mount, library, db_path)
            self.assertEqual((summary.copied, summary.reused), (0, 1))
            self.assertEqual(recording_row(db_path)[7], "SYNCED")

    def test_existing_final_mismatch_is_conflict_and_unchanged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount, b"manifest-content")
            final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
            final.parent.mkdir(parents=True)
            original = b"preexisting-different-content"
            final.write_bytes(original)
            summary = self.run_sync(mount, library, db_path)
            self.assertEqual((summary.copied, summary.conflicts), (0, 1))
            self.assertEqual(final.read_bytes(), original)
            self.assertEqual(recording_row(db_path)[7], "CONFLICT")

    def test_same_id_changed_hash_is_conflict_without_raw_overwrite(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount, b"first")
            self.run_sync(mount, library, db_path)
            final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
            original = final.read_bytes()
            changed = b"second-different"
            (mount/"M5DAYLOG"/"recordings"/"source.wav").write_bytes(changed)
            def mutate(doc):
                item = doc["recordings"][0]
                item["sizeBytes"] = len(changed)
                item["sha256"] = digest(changed)
            rewrite_manifest(mount, mutate)
            summary = self.run_sync(mount, library, db_path)
            self.assertEqual(summary.conflicts, 1)
            self.assertEqual(final.read_bytes(), original)

    def test_stale_partial_restarts_from_zero(self):
        payload = b"0123456789" * 128
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount, payload)
            final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
            partial = Path(str(final) + ".partial")
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"stale")
            summary = self.run_sync(mount, library, db_path)
            self.assertEqual(summary.copied, 1)
            self.assertEqual(final.read_bytes(), payload)
            self.assertFalse(partial.exists())

    def test_25_50_75_percent_interruptions_recover(self):
        sync = sync_module()
        payload = bytes(range(256)) * 64
        for ratio in (0.25, 0.50, 0.75):
            with self.subTest(ratio=ratio), TemporaryDirectory() as tmp:
                root = Path(tmp)
                mount, library, db_path = root/"drive", root/"lib", root/"state.db"
                write_device(mount, payload)
                final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
                partial = Path(str(final) + ".partial")
                partial.parent.mkdir(parents=True)
                limit = int(len(payload) * ratio)
                with self.assertRaises(OSError):
                    sync.copy_verified_stream(
                        InterruptingReader(payload, limit),
                        partial,
                        expected_size=len(payload),
                        expected_sha256=digest(payload),
                        chunk_size=257,
                    )
                self.assertEqual(partial.stat().st_size, limit)
                summary = self.run_sync(mount, library, db_path)
                self.assertEqual(summary.copied, 1)
                self.assertEqual(digest(final.read_bytes()), digest(payload))
                con = sqlite3.connect(str(db_path))
                try:
                    count = con.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
                finally:
                    con.close()
                self.assertEqual(count, 1)

    def test_final_before_db_commit_converges_without_recopy(self):
        payload = b"synthetic-post-rename-crash"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount, payload)
            final = library/"2026"/"10"/"03"/"raw"/f"{RECORDING_ID}.wav"
            final.parent.mkdir(parents=True)
            final.write_bytes(payload)
            db.init_db(db_path)
            self.assertIsNone(recording_row(db_path))
            summary = self.run_sync(mount, library, db_path)
            self.assertEqual((summary.copied, summary.reused), (0, 1))
            self.assertEqual(recording_row(db_path)[7], "SYNCED")

    def test_publish_no_clobber_preserves_existing_final(self):
        sync = sync_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial, final = root/"x.wav.partial", root/"x.wav"
            partial.write_bytes(b"new")
            final.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                sync.publish_no_clobber(partial, final)
            self.assertEqual(final.read_bytes(), b"existing")


class CliTests(unittest.TestCase):
    def test_help_lists_device_and_sync(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("device", stdout.getvalue())
        self.assertIn("sync", stdout.getvalue())

    def test_sync_output_is_metadata_counts_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount, library, db_path = root/"drive", root/"lib", root/"state.db"
            write_device(mount, b"synthetic-private-looking-audio")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main([
                    "sync", str(mount),
                    "--library-root", str(library),
                    "--db", str(db_path),
                    "--contracts-dir", str(CONTRACTS),
                ])
            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("copied=1", output)
            self.assertNotIn("source.wav", output)
            self.assertNotIn("synthetic-private-looking-audio", output)


if __name__ == "__main__":
    unittest.main()
