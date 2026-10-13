"""#117 production-C manifest fault matrix; synthetic data, no device writes.

Builds the actual recorder C sources with a host-only FAT rename emulator.
This tests code paths rather than a Python mirror. Real ESP-IDF/FAT32 and
power-loss durability remain separate exact-head acceptance requirements.
"""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"
INCLUDE = COMP / "include"
DEVICE_ID = "123e4567-e89b-42d3-a456-426614174000"
NOW = "2026-10-13T12:00:00+00:00"

# Hook only the manifest update's rename calls. Normal host rename can
# overwrite a destination; FAT cannot. This deterministic adapter fails
# if the target already exists, and can inject EIO at chosen call numbers.
RENAME_EMULATOR = r"""
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <sys/stat.h>

static int calls = 0;

void manifest_test_reset(void) { calls = 0; }
int manifest_test_calls(void) { return calls; }

static int fault_at(int call) {
    const char *p = getenv("M5_TEST_FAIL_RENAME");
    if (!p) return 0;
    while (*p) {
        char *end = NULL;
        long v = strtol(p, &end, 10);
        if (end == p) return 0;
        if (v == call) return 1;
        p = (*end == ',') ? end + 1 : end;
    }
    return 0;
}

int manifest_test_rename(const char *from, const char *to) {
    struct stat st;
    ++calls;
    if (fault_at(calls)) {
        errno = EIO;
        return -1;
    }
    if (stat(to, &st) == 0) {
        errno = EEXIST;
        return -1;
    }
    return rename(from, to);
}
"""


@pytest.fixture(scope="module")
def native_manifest(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.skip("native C compiler unavailable")
    work = tmp_path_factory.mktemp("manifest-native-117")
    wrapper = work / "fat_rename.c"
    wrapper.write_text(RENAME_EMULATOR, encoding="utf-8")
    objects = []
    for source in ("device_manifest.c", "device_identity.c", "sha256.c"):
        obj = work / (source + ".o")
        subprocess.run(
            [cc, "-std=c11", "-fPIC", "-O0", "-I", str(INCLUDE),
             "-Drename=manifest_test_rename", "-c", str(COMP / source),
             "-o", str(obj)],
            check=True, capture_output=True, text=True,
        )
        objects.append(str(obj))
    shim_obj = work / "fat_rename.o"
    subprocess.run(
        [cc, "-std=c11", "-fPIC", "-c", str(wrapper), "-o", str(shim_obj)],
        check=True, capture_output=True, text=True,
    )
    dll = work / "manifest_117.so"
    subprocess.run(
        [cc, "-shared", *objects, str(shim_obj), "-o", str(dll)],
        check=True, capture_output=True, text=True,
    )
    lib = ctypes.CDLL(str(dll))
    lib.manifest_test_reset.argtypes = []
    lib.manifest_test_calls.restype = ctypes.c_int
    lib.device_manifest_upsert_file.argtypes = [ctypes.c_char_p] * 5
    lib.device_manifest_upsert_file.restype = ctypes.c_int
    lib.device_manifest_recover_tmp.argtypes = [ctypes.c_char_p] * 3
    lib.device_manifest_recover_tmp.restype = ctypes.c_bool
    lib.device_manifest_sync_wav_dir.argtypes = (
        [ctypes.c_char_p] * 6 +
        [ctypes.POINTER(ctypes.c_uint32)] * 2
    )
    lib.device_manifest_sync_wav_dir.restype = ctypes.c_bool
    return lib


def _b(value):
    return os.fsencode(value)


def _entry(n, sha=None):
    rid = f"123e4567-e89b-42d3-a456-{n:012x}"
    filename = f"recordings/2026-10-13/120000_{rid}.wav"
    return dict(
        recordingId=rid, filename=filename, startedAt=NOW,
        durationMs=0, sizeBytes=44,
        sha256=sha or hashlib.sha256(bytes([n % 256]) * 44).hexdigest(),
        sampleRate=16000, bitDepth=16, channels=1,
        state="recovered", firmwareVersion="0.1.0",
    )


def _manifest(entries):
    return json.dumps(
        dict(schemaVersion=1, deviceId=DEVICE_ID, updatedAt=NOW,
             recordings=entries),
        separators=(",", ":"),
    ).encode()


def _create(tmp_path, entries=()):
    final, temp = tmp_path / "manifest.json", tmp_path / "manifest.tmp"
    final.write_bytes(_manifest(list(entries)))
    return final, temp


def _upsert(lib, final, temp, entry):
    return lib.device_manifest_upsert_file(
        _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _manifest_entry(entry),
    )


def _manifest_entry(entry):
    return json.dumps(entry, separators=(",", ":")).encode()


def _recover(lib, final, temp):
    return lib.device_manifest_recover_tmp(
        _b(final), _b(temp), _b(DEVICE_ID)
    )


def test_fat_replace_retains_complete_history(native_manifest, tmp_path, monkeypatch):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    old = _entry(1)
    final, temp = _create(tmp_path, [old])
    native_manifest.manifest_test_reset()
    assert _upsert(native_manifest, final, temp, _entry(2)) == 0
    assert native_manifest.manifest_test_calls() == 3
    assert (tmp_path / "manifest.json.bak").read_bytes() == _manifest([old])
    assert _recover(native_manifest, final, temp)
    assert not (tmp_path / "manifest.json.bak").exists()
    rows = json.loads(final.read_bytes())["recordings"]
    assert rows == [old, _entry(2)]


@pytest.mark.parametrize("fail_calls", ["1", "2", "3", "3,4"])
def test_rename_failures_do_not_discard_m0(
    native_manifest, tmp_path, monkeypatch, fail_calls
):
    old = _entry(1)
    final, temp = _create(tmp_path, [old])
    m0 = final.read_bytes()
    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_RENAME", fail_calls)
    assert _upsert(native_manifest, final, temp, _entry(2)) == 2
    assert final.exists() or (tmp_path / "manifest.json.bak").exists()
    monkeypatch.delenv("M5_TEST_FAIL_RENAME")
    assert _recover(native_manifest, final, temp)
    assert final.read_bytes() == m0
    assert not temp.exists()
    assert not (tmp_path / "manifest.json.bak").exists()


def test_corrupt_published_final_never_promotes_stale_tmp(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [_entry(1)])
    final.write_bytes(b'{"schemaVersion":1,"deviceId":')
    temp.write_bytes(_manifest([_entry(2)]))
    native_manifest.manifest_test_reset()
    assert not _recover(native_manifest, final, temp)
    assert final.read_bytes() == b'{"schemaVersion":1,"deviceId":'
    assert temp.exists()


def test_bad_candidate_with_backup_fails_closed(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [_entry(1)])
    backup = tmp_path / "manifest.json.bak"
    backup.write_bytes(final.read_bytes())
    final.write_bytes(_manifest([_entry(2)]))  # not an M0 superset
    native_manifest.manifest_test_reset()
    assert not _recover(native_manifest, final, temp)
    assert backup.exists()
    assert json.loads(final.read_bytes())["recordings"] == [_entry(2)]


def test_unindexed_canonical_wav_conflict_fails_boot_scan(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    directory = tmp_path / "recordings" / "2026-10-13"
    directory.mkdir(parents=True)
    rec = _entry(1)
    wav = directory / Path(rec["filename"]).name
    wav.write_bytes(b"x" * 44)
    # Identical ID but an old committed different hash: no RAW overwrite.
    final.write_bytes(_manifest([rec]))
    n_added, n_skipped = ctypes.c_uint32(), ctypes.c_uint32()
    native_manifest.manifest_test_reset()
    assert not native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(n_added), ctypes.byref(n_skipped),
    )
    assert final.read_bytes() == _manifest([rec])
    assert wav.read_bytes() == b"x" * 44


def test_unreadable_canonical_wav_is_not_skipped_as_healthy(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    directory = tmp_path / "recordings" / "2026-10-13"
    directory.mkdir(parents=True)
    rec = _entry(1)
    (directory / Path(rec["filename"]).name).symlink_to("missing.wav")
    n_added, n_skipped = ctypes.c_uint32(), ctypes.c_uint32()
    native_manifest.manifest_test_reset()
    assert not native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(n_added), ctypes.byref(n_skipped),
    )
    assert final.read_bytes() == _manifest([])


def test_invalid_legacy_name_remains_excluded_not_fatal(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    directory = tmp_path / "recordings" / "2026-10-13"
    directory.mkdir(parents=True)
    (directory / "legacy.wav").write_bytes(b"x" * 44)
    added, skipped = ctypes.c_uint32(), ctypes.c_uint32()
    assert native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(added), ctypes.byref(skipped),
    )
    assert added.value == 0
    assert skipped.value >= 1


def test_manifest_limit_refuses_extra_uuid_without_losing_m0(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    # Produce a complete canonical M0 just below the compiled 131072B
    # ceiling; another entry would exceed it. No padding or invalid JSON.
    entries = []
    while len(_manifest(entries + [_entry(len(entries) + 1)])) < 130900:
        entries.append(_entry(len(entries) + 1))
    final, temp = _create(tmp_path, entries)
    before = final.read_bytes()
    assert len(before) < 131072
    assert len(before) + len(_manifest_entry(_entry(999))) > 131072
    native_manifest.manifest_test_reset()
    assert _upsert(native_manifest, final, temp, _entry(999)) == 2
    assert final.read_bytes() == before
