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
#include <unistd.h>
#include <dirent.h>

static int calls = 0;
static int flush_calls = 0, close_calls = 0, sync_calls = 0;
static int remove_calls = 0, write_calls = 0, readdir_calls = 0;
void manifest_test_reset(void) {
    calls = 0;
    flush_calls = close_calls = sync_calls = remove_calls = write_calls = 0;
    readdir_calls = 0;
}
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

static int io_call(const char *name, int *counter) {
    const char *p = getenv(name);
    *counter += 1;
    if (!p) return 0;
    return atoi(p) == *counter;
}

int manifest_test_fflush(FILE *fp) {
    if (io_call("M5_TEST_FAIL_FFLUSH", &flush_calls)) {
        errno = EIO;
        return EOF;
    }
    return fflush(fp);
}
int manifest_test_fclose(FILE *fp) {
    int ret = fclose(fp);
    if (io_call("M5_TEST_FAIL_FCLOSE", &close_calls)) {
        errno = EIO;
        return EOF;
    }
    return ret;
}
int manifest_test_fsync(int fd) {
    if (io_call("M5_TEST_FAIL_FSYNC", &sync_calls)) {
        errno = EIO;
        return -1;
    }
    return fsync(fd);
}
int manifest_test_remove(const char *path) {
    if (io_call("M5_TEST_FAIL_REMOVE", &remove_calls)) {
        errno = EIO;
        return -1;
    }
    return remove(path);
}
size_t manifest_test_fwrite(const void *ptr, size_t size, size_t nmemb,
                            FILE *fp) {
    ++write_calls;
    if (getenv("M5_TEST_FAIL_FWRITE") &&
        atoi(getenv("M5_TEST_FAIL_FWRITE")) == write_calls && nmemb > 0) {
        return fwrite(ptr, size, nmemb - 1u, fp);
    }
    return fwrite(ptr, size, nmemb, fp);
}
struct dirent *manifest_test_readdir(DIR *dir) {
    ++readdir_calls;
    if (getenv("M5_TEST_FAIL_READDIR") &&
        atoi(getenv("M5_TEST_FAIL_READDIR")) == readdir_calls) {
        errno = EIO;
        return NULL;
    }
    return readdir(dir);
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
             "-DDEVICE_MANIFEST_NATIVE_SYNC_TEST",
             "-Drename=manifest_test_rename",
             "-Dfflush=manifest_test_fflush",
             "-Dfclose=manifest_test_fclose",
             "-Dfsync=manifest_test_fsync",
             "-Dremove=manifest_test_remove",
             "-Dfwrite=manifest_test_fwrite",
             "-Dreaddir=manifest_test_readdir",
             "-c", str(COMP / source),
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
    lib.device_manifest_has_append_room.argtypes = [ctypes.c_char_p] * 2
    lib.device_manifest_has_append_room.restype = ctypes.c_bool
    lib.device_manifest_last_scan_error.argtypes = []
    lib.device_manifest_last_scan_error.restype = ctypes.c_int
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
    assert native_manifest.device_manifest_last_scan_error() == 4
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
    while len(_manifest(entries + [_entry(len(entries) + 1)])) < 131072:
        entries.append(_entry(len(entries) + 1))
    final, temp = _create(tmp_path, entries)
    before = final.read_bytes()
    assert len(before) < 131072
    assert len(before) + len(_manifest_entry(_entry(999))) > 131072
    native_manifest.manifest_test_reset()
    assert _upsert(native_manifest, final, temp, _entry(999)) == 2
    assert final.read_bytes() == before


@pytest.mark.parametrize(
    "fault,call",
    [
        ("M5_TEST_FAIL_FWRITE", "1"),
        ("M5_TEST_FAIL_FFLUSH", "1"),
        ("M5_TEST_FAIL_FSYNC", "1"),
    ],
)
def test_failed_temp_write_flush_or_fsync_never_retires_m0(
    native_manifest, tmp_path, monkeypatch, fault, call
):
    old = _entry(1)
    final, temp = _create(tmp_path, [old])
    m0 = final.read_bytes()
    monkeypatch.setenv(fault, call)
    native_manifest.manifest_test_reset()
    assert _upsert(native_manifest, final, temp, _entry(2)) == 2
    assert final.read_bytes() == m0
    monkeypatch.delenv(fault)


def test_failed_backup_cleanup_preserves_both_snapshots(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    old = _entry(1)
    final, temp = _create(tmp_path, [old])
    native_manifest.manifest_test_reset()
    assert _upsert(native_manifest, final, temp, _entry(2)) == 0
    bak = tmp_path / "manifest.json.bak"
    assert bak.exists()
    monkeypatch.setenv("M5_TEST_FAIL_REMOVE", "1")
    assert not _recover(native_manifest, final, temp)
    assert bak.exists()
    assert len(json.loads(final.read_bytes())["recordings"]) == 2
    monkeypatch.delenv("M5_TEST_FAIL_REMOVE")
    assert _recover(native_manifest, final, temp)
    assert not bak.exists()


def test_multiple_fat_appends_preserve_every_uuid(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [_entry(1)])
    native_manifest.manifest_test_reset()
    assert _upsert(native_manifest, final, temp, _entry(2)) == 0
    assert (tmp_path / "manifest.json.bak").exists()
    assert _upsert(native_manifest, final, temp, _entry(3)) == 0
    assert [x["recordingId"] for x in json.loads(final.read_bytes())["recordings"]] == [
        _entry(1)["recordingId"], _entry(2)["recordingId"],
        _entry(3)["recordingId"],
    ]
    assert _recover(native_manifest, final, temp)


def test_wrong_size_same_uuid_same_hash_fails_closed(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    directory = tmp_path / "recordings" / "2026-10-13"
    directory.mkdir(parents=True)
    rec = _entry(1, hashlib.sha256(b"x" * 44).hexdigest())
    wav = directory / Path(rec["filename"]).name
    wav.write_bytes(b"x" * 44)
    stale = dict(rec, sizeBytes=46)
    final.write_bytes(_manifest([stale]))
    before = final.read_bytes()
    added, skipped = ctypes.c_uint32(), ctypes.c_uint32()
    native_manifest.manifest_test_reset()
    assert not native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(added), ctypes.byref(skipped),
    )
    assert final.read_bytes() == before
    assert wav.read_bytes() == b"x" * 44


def test_metadata_admission_guard_covers_queued_rotation(
    native_manifest, tmp_path
):
    final, temp = _create(tmp_path, [])
    assert native_manifest.device_manifest_has_append_room(
        _b(final), _b(DEVICE_ID)
    )
    rows = []
    while len(_manifest(rows + [_entry(len(rows) + 1)])) < 130000:
        rows.append(_entry(len(rows) + 1))
    final.write_bytes(_manifest(rows))
    assert not native_manifest.device_manifest_has_append_room(
        _b(final), _b(DEVICE_ID)
    )
    assert all(r["recordingId"] for r in rows)


def test_close_error_does_not_retire_m0(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [_entry(1)])
    m0 = final.read_bytes()
    native_manifest.manifest_test_reset()
    # Read/close existing M0 is call 1; temp close is call 2.
    monkeypatch.setenv("M5_TEST_FAIL_FCLOSE", "2")
    assert _upsert(native_manifest, final, temp, _entry(2)) == 2
    assert final.read_bytes() == m0
    monkeypatch.delenv("M5_TEST_FAIL_FCLOSE")


def test_readdir_eio_cannot_produce_healthy_index_scan(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    directory = tmp_path / "recordings" / "2026-10-13"
    directory.mkdir(parents=True)
    rec = _entry(1)
    wav = directory / Path(rec["filename"]).name
    wav.write_bytes(b"x" * 44)
    before = final.read_bytes()
    added, skipped = ctypes.c_uint32(), ctypes.c_uint32()
    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_READDIR", "2")
    assert not native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(added), ctypes.byref(skipped),
    )
    monkeypatch.delenv("M5_TEST_FAIL_READDIR")
    assert final.read_bytes() == before
    assert wav.read_bytes() == b"x" * 44


def test_poc_c_default_nvs_and_ack_negatives_are_not_enabled():
    identity = (COMP / "device_identity.c").read_text(encoding="utf-8")
    nvs = (COMP / "recorder_nvs.c").read_text(encoding="utf-8")
    runtime = (ROOT / "firmware/main/task49_runtime.c").read_text(
        encoding="utf-8"
    )
    # This is a source-level regression. It is not a runtime or physical
    # no-mutation certificate, which remains a separate release gate.
    assert "nvs_flash_erase(" not in identity
    assert "nvs_flash_erase(" not in nvs
    assert "processed_ack.h" not in runtime
    assert "processed_ack_apply(" not in runtime


def test_scan_failure_classification_survives_for_operator_diagnostics(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    directory = tmp_path / "recordings" / "2026-10-13"
    directory.mkdir(parents=True)
    wav = directory / Path(_entry(1)["filename"]).name
    wav.write_bytes(b"x" * 44)
    added, skipped = ctypes.c_uint32(), ctypes.c_uint32()
    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_READDIR", "2")
    assert not native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(added), ctypes.byref(skipped),
    )
    assert native_manifest.device_manifest_last_scan_error() == 2  # DIRECTORY_ERROR
    monkeypatch.delenv("M5_TEST_FAIL_READDIR")
    native_manifest.manifest_test_reset()
    # Clean retry converges and clears the previous failure status.
    assert native_manifest.device_manifest_sync_wav_dir(
        _b(directory.parent), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(added), ctypes.byref(skipped),
    )
    assert native_manifest.device_manifest_last_scan_error() == 0


def test_missing_manifest_cannot_reset_nonempty_recording_history():
    # Source integration check for the ESP-only recorder startup path.
    # FAT32 behavior and injected power loss remain a separate physical gate.
    main = (ROOT / "firmware/main/main.c").read_text(encoding="utf-8")
    ensure = main.split("static bool recorder_ensure_manifest_exists(void)", 1)[1]
    ensure = ensure.split("static void recorder_battery_task", 1)[0]
    assert "recorder_recordings_tree_pristine()" in ensure
    assert ensure.index("recorder_recordings_tree_pristine()") < ensure.index(
        "device_manifest_build_empty("
    )
    assert "MANIFEST_RECOVERY_REQUIRED" in ensure
