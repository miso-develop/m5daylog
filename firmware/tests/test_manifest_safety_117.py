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
import struct
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMP = ROOT / "firmware/components/recorder"
INCLUDE = COMP / "include"
DEVICE_ID = "123e4567-e89b-42d3-a456-426614174000"
NOW = "2026-10-13T12:00:00+00:00"


def _wav(pcm=b""):
    assert len(pcm) % 2 == 0
    return (
        b"RIFF" + struct.pack("<I", len(pcm) + 36) +
        b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
        b"data" + struct.pack("<I", len(pcm)) + pcm
    )

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
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Instrument allocations issued by the compiled production-C path.
typedef union { max_align_t align; size_t bytes; } manifest_alloc_hdr;
static size_t mem_live = 0, mem_peak = 0, mem_limit = SIZE_MAX;
void manifest_test_memory_limit(size_t limit) {
    mem_limit = limit;
    mem_peak = mem_live;
}
size_t manifest_test_memory_peak(void) { return mem_peak; }
size_t manifest_test_memory_live(void) { return mem_live; }
void *manifest_test_malloc(size_t count) {
    manifest_alloc_hdr *h;
    if (count > mem_limit - mem_live ||
        count > SIZE_MAX - sizeof(*h)) return NULL;
    h = (manifest_alloc_hdr *)malloc(sizeof(*h) + count);
    if (!h) return NULL;
    h->bytes = count;
    mem_live += count;
    if (mem_live > mem_peak) mem_peak = mem_live;
    return h + 1;
}
void manifest_test_free(void *ptr) {
    manifest_alloc_hdr *h;
    if (!ptr) return;
    h = (manifest_alloc_hdr *)ptr - 1;
    mem_live -= h->bytes;
    free(h);
}

static int calls = 0;
static int flush_calls = 0, close_calls = 0, sync_calls = 0;
static int remove_calls = 0, write_calls = 0, readdir_calls = 0;
static int stat_match_calls = 0;
int manifest_test_stat_match_calls(void) { return stat_match_calls; }

// Intercept actual C stat() calls without rewriting the struct stat tag.
// Fault only a selected exact path and nth match; libc calls stay intact.
int __real_stat(const char *path, struct stat *st);
int __wrap_stat(const char *path, struct stat *st) {
    const char *match = getenv("M5_TEST_FAIL_STAT_MATCH");
    if (match != NULL && strcmp(path, match) == 0) {
        const char *at = getenv("M5_TEST_FAIL_STAT_AT");
        stat_match_calls++;
        if (at == NULL || atoi(at) == stat_match_calls) {
            errno = EIO;
            return -1;
        }
    }
    return __real_stat(path, st);
}

void manifest_test_reset(void) {
    calls = 0;
    flush_calls = close_calls = sync_calls = remove_calls = write_calls = 0;
    readdir_calls = 0;
    stat_match_calls = 0;
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
             "-Dmalloc=manifest_test_malloc",
             "-Dfree=manifest_test_free",
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
        [cc, "-shared", "-Wl,--wrap=stat", *objects, str(shim_obj),
         "-o", str(dll)],
        check=True, capture_output=True, text=True,
    )
    lib = ctypes.CDLL(str(dll))
    lib.manifest_test_reset.argtypes = []
    lib.manifest_test_calls.restype = ctypes.c_int
    lib.manifest_test_stat_match_calls.argtypes = []
    lib.manifest_test_stat_match_calls.restype = ctypes.c_int
    lib.manifest_test_memory_limit.argtypes = [ctypes.c_size_t]
    lib.manifest_test_memory_peak.restype = ctypes.c_size_t
    lib.manifest_test_memory_live.restype = ctypes.c_size_t
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
    wav.write_bytes(_wav())
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
    assert wav.read_bytes() == _wav()


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
    rec = _entry(1, hashlib.sha256(_wav()).hexdigest())
    wav = directory / Path(rec["filename"]).name
    wav.write_bytes(_wav())
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
    assert wav.read_bytes() == _wav()


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
    wav.write_bytes(_wav())
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
    assert wav.read_bytes() == _wav()


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
    wav.write_bytes(_wav())
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


def _scan(native_manifest, directory, final, temp):
    added, excluded = ctypes.c_uint32(), ctypes.c_uint32()
    ok = native_manifest.device_manifest_sync_wav_dir(
        _b(directory), _b(final), _b(temp), _b(DEVICE_ID), _b(NOW),
        _b("recovered"), ctypes.byref(added), ctypes.byref(excluded),
    )
    return ok, added.value, excluded.value


def test_reverse_completeness_missing_committed_wav_fails_and_recovers(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    contents = _wav(b"\0" * 64)
    rows = [_entry(n, hashlib.sha256(contents).hexdigest()) for n in (1, 2)]
    for row in rows:
        row["sizeBytes"] = len(contents)
        row["durationMs"] = 2
    final, temp = _create(tmp_path, rows)
    before = final.read_bytes()
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    (date_dir / Path(rows[0]["filename"]).name).write_bytes(contents)

    for _ in range(2):  # restart/replay must never produce healthy inventory
        native_manifest.manifest_test_reset()
        ok, added, _ = _scan(native_manifest, recordings, final, temp)
        assert not ok
        assert added == 0
        assert native_manifest.device_manifest_last_scan_error() == 6  # MISSING_WAV
        assert final.read_bytes() == before
        assert len(list(date_dir.glob("*.wav"))) == 1

    (date_dir / Path(rows[1]["filename"]).name).write_bytes(contents)
    native_manifest.manifest_test_reset()
    ok, added, excluded = _scan(native_manifest, recordings, final, temp)
    assert ok and added == 0 and excluded == 0
    assert native_manifest.device_manifest_last_scan_indexed() == 2
    assert final.read_bytes() == before


@pytest.mark.parametrize("damage", [
    "not_riff", "declared_riff_size", "declared_data_size", "format", "truncated",
    "unaligned",
])
def test_corrupt_finalized_wav_is_never_indexed(
    native_manifest, tmp_path, monkeypatch, damage
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    wav = date_dir / Path(_entry(1)["filename"]).name
    raw = bytearray(_wav(b"\0" * 64))
    if damage == "not_riff":
        raw[:4] = b"JUNK"
    elif damage == "declared_riff_size":
        raw[4:8] = struct.pack("<I", len(raw) - 6)
    elif damage == "declared_data_size":
        raw[40:44] = struct.pack("<I", 66)
    elif damage == "format":
        raw[20:22] = struct.pack("<H", 3)
    elif damage == "truncated":
        del raw[-2:]
    else:
        raw.append(0)
    wav.write_bytes(raw)
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp)[0] is False
    assert native_manifest.device_manifest_last_scan_error() == 3  # WAV_UNREADABLE
    assert json.loads(final.read_bytes())["recordings"] == []
    assert wav.read_bytes() == raw


def test_valid_finalized_wav_new_index_and_noop_replay_counts(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    final, temp = _create(tmp_path, [])
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    raw = _wav(b"\0" * 32000)
    wav = date_dir / Path(_entry(1)["filename"]).name
    wav.write_bytes(raw)
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp) == (True, 1, 0)
    assert native_manifest.device_manifest_last_scan_indexed() == 1
    first = json.loads(final.read_bytes())["recordings"]
    assert len(first) == 1
    assert first[0]["sizeBytes"] == len(raw)
    assert first[0]["durationMs"] == 1000
    assert first[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert _recover(native_manifest, final, temp)
    snapshot = final.read_bytes()
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp) == (True, 0, 0)
    assert native_manifest.device_manifest_last_scan_indexed() == 1
    assert final.read_bytes() == snapshot


def test_committed_duration_disagreement_stays_fail_closed_on_replay(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    raw = _wav(b"\0" * 32000)
    old = _entry(1, hashlib.sha256(raw).hexdigest())
    old["sizeBytes"] = len(raw)
    old["durationMs"] = 1800000  # corrupt pre-existing duration; never rewrite M0
    final, temp = _create(tmp_path, [old])
    before = final.read_bytes()
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    wav = date_dir / Path(old["filename"]).name
    wav.write_bytes(raw)
    for _ in range(2):
        native_manifest.manifest_test_reset()
        ok, _, _ = _scan(native_manifest, recordings, final, temp)
        assert not ok
        assert native_manifest.device_manifest_last_scan_error() == 5  # INDEX_WRITE_ERROR
        assert final.read_bytes() == before
        assert wav.read_bytes() == raw


def test_large_accepted_transaction_bounded_heap_restart(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    rows = []
    while len(_manifest(rows + [_entry(len(rows) + 1)])) < 29500:
        rows.append(_entry(len(rows) + 1))
    final, temp = _create(tmp_path, rows)
    assert native_manifest.device_manifest_has_append_room(
        _b(final), _b(DEVICE_ID)
    )
    before = final.read_bytes()
    incoming = _entry(len(rows) + 1)
    native_manifest.manifest_test_reset()
    # The old four-snapshot algorithm exceeds this budget; at most three
    # live snapshots are needed by the modified production C.
    native_manifest.manifest_test_memory_limit(112 * 1024)
    try:
        assert _upsert(native_manifest, final, temp, incoming) == 0
        peak = native_manifest.manifest_test_memory_peak()
        assert peak <= 112 * 1024
        assert native_manifest.manifest_test_memory_live() == 0
        assert _recover(native_manifest, final, temp)
        assert json.loads(final.read_bytes())["recordings"] == rows + [incoming]
        assert len(final.read_bytes()) > len(before)
        assert native_manifest.manifest_test_memory_live() == 0
    finally:
        native_manifest.manifest_test_memory_limit(2**63 - 1)


def test_low_memory_append_fail_closed_preserves_m0(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    rows = []
    while len(_manifest(rows + [_entry(len(rows) + 1)])) < 29500:
        rows.append(_entry(len(rows) + 1))
    final, temp = _create(tmp_path, rows)
    before = final.read_bytes()
    native_manifest.manifest_test_reset()
    native_manifest.manifest_test_memory_limit(50 * 1024)
    try:
        assert _upsert(native_manifest, final, temp, _entry(len(rows) + 1)) == 2
        assert final.read_bytes() == before
        assert native_manifest.manifest_test_memory_live() == 0
    finally:
        native_manifest.manifest_test_memory_limit(2**63 - 1)
    native_manifest.manifest_test_reset()
    assert _recover(native_manifest, final, temp)
    assert final.read_bytes() == before


@pytest.mark.parametrize("started_at", [
    "2026-10-13T18:00:00+00:00",
    "2026-10-13T12:00:03+00:00",
    "2026-10-13T12:00:00+01:00",
])
def test_replay_timestamp_conflict_repeat_restart_and_recover(
    native_manifest, tmp_path, monkeypatch, started_at
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    data = _wav(b"\0" * 64)
    entry = _entry(1, hashlib.sha256(data).hexdigest())
    entry.update(startedAt=started_at, sizeBytes=len(data), durationMs=2)
    final, temp = _create(tmp_path, [entry])
    wav = date_dir / Path(entry["filename"]).name
    wav.write_bytes(data)
    original = final.read_bytes()
    for _ in range(2):
        native_manifest.manifest_test_reset()
        assert _scan(native_manifest, recordings, final, temp)[0] is False
        assert native_manifest.device_manifest_last_scan_error() == 4
        assert final.read_bytes() == original and wav.read_bytes() == data
    entry["startedAt"] = NOW
    final.write_bytes(_manifest([entry]))
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp) == (True, 0, 0)


@pytest.mark.parametrize("started_at", [
    "2026-10-13T11:59:58+00:00",
    "2026-10-13T13:00:00+01:00",
    "2026-10-13T12:00:02+00:00",
])
def test_replay_permits_bounded_clock_skew(
    native_manifest, tmp_path, monkeypatch, started_at
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    data = _wav(b"\0" * 64)
    entry = _entry(1, hashlib.sha256(data).hexdigest())
    entry.update(startedAt=started_at, sizeBytes=len(data), durationMs=2)
    final, temp = _create(tmp_path, [entry])
    (date_dir / Path(entry["filename"]).name).write_bytes(data)
    before = final.read_bytes()
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp) == (True, 0, 0)
    assert final.read_bytes() == before


def test_replay_midnight_second_boundary(
    native_manifest, tmp_path, monkeypatch
):
    monkeypatch.delenv("M5_TEST_FAIL_RENAME", raising=False)
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-14"
    date_dir.mkdir(parents=True)
    data = _wav(b"\0" * 64)
    entry = _entry(1, hashlib.sha256(data).hexdigest())
    entry.update(
        filename=entry["filename"].replace(
            "2026-10-13/120000_", "2026-10-14/000000_"
        ),
        startedAt="2026-10-13T23:59:59+00:00",
        sizeBytes=len(data), durationMs=2,
    )
    final, temp = _create(tmp_path, [entry])
    (date_dir / Path(entry["filename"]).name).write_bytes(data)
    before = final.read_bytes()
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp) == (True, 0, 0)
    assert final.read_bytes() == before


def test_device_admission_checks_internal_heap_and_same_clock_snapshot():
    # Source integration for ESP-only recorder control flow. Native-C tests
    # exercise the transaction; target RAM watermark measurement remains HG.
    main = (ROOT / "firmware/main/main.c").read_text(encoding="utf-8")
    admission = main.split("static bool recorder_can_admit_segment(void)", 1)[1]
    admission = admission.split("static void recorder_battery_task", 1)[0]
    assert "heap_caps_get_free_size(MALLOC_CAP_8BIT)" in admission
    assert "heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)" in admission
    assert "DEVICE_MANIFEST_HEAP_ADMISSION_BYTES" in admission
    assert "METADATA_HEAP" in admission
    assert "recorder_iso8601_from_parts(seg_date, cur_time," in main
    rotation = main.split("if (reason == WAV_ROTATE_TIME_30MIN ||", 1)[1]
    assert rotation.index("recorder_manifest_record_wav(") < rotation.index(
        "recorder_current_date_time(now_date, now_time);"
    )
    assert "recorder_iso8601_from_parts(now_date, now_time," in rotation


@pytest.mark.parametrize("artifact", [
    "manifest.json", "manifest.tmp", "manifest.json.bak",
])
def test_artifact_stat_eio_is_not_interpreted_as_absence(
    native_manifest, tmp_path, monkeypatch, artifact
):
    final, temp = _create(tmp_path, [_entry(1)])
    backup = tmp_path / "manifest.json.bak"
    backup.write_bytes(final.read_bytes())
    temp.write_bytes(_manifest([_entry(1), _entry(2)]))
    originals = {p: p.read_bytes() for p in (final, temp, backup)}

    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_STAT_MATCH",
                       os.fspath(tmp_path / artifact))
    monkeypatch.setenv("M5_TEST_FAIL_STAT_AT", "1")
    assert not _recover(native_manifest, final, temp)
    assert native_manifest.manifest_test_stat_match_calls() == 1
    assert {p: p.read_bytes() for p in originals} == originals

    monkeypatch.delenv("M5_TEST_FAIL_STAT_MATCH")
    monkeypatch.delenv("M5_TEST_FAIL_STAT_AT")
    native_manifest.manifest_test_reset()
    assert _recover(native_manifest, final, temp)
    assert final.read_bytes() == originals[final]
    assert not backup.exists() and not temp.exists()


def test_backup_stat_eio_cannot_hide_m0_regression(
    native_manifest, tmp_path, monkeypatch
):
    # M0 proves that A+B were committed. A separately valid but regressed
    # final A cannot supersede it even if the backup lookup hits EIO.
    wav_data = _wav(b"\\0" * 64)
    wav_hash = hashlib.sha256(wav_data).hexdigest()
    rows = [_entry(i, wav_hash) for i in (1, 2)]
    for row in rows:
        row["sizeBytes"] = len(wav_data)
        row["durationMs"] = 2
    final, temp = _create(tmp_path, rows[:1])
    backup = tmp_path / "manifest.json.bak"
    backup.write_bytes(_manifest(rows))
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    wav_a = date_dir / Path(rows[0]["filename"]).name
    wav_a.write_bytes(wav_data)
    final_before, backup_before = final.read_bytes(), backup.read_bytes()
    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_STAT_MATCH", os.fspath(backup))
    monkeypatch.setenv("M5_TEST_FAIL_STAT_AT", "1")
    assert not _recover(native_manifest, final, temp)
    assert native_manifest.manifest_test_stat_match_calls() == 1
    assert final.read_bytes() == final_before
    assert backup.read_bytes() == backup_before
    assert wav_a.read_bytes() == wav_data

    monkeypatch.delenv("M5_TEST_FAIL_STAT_MATCH")
    monkeypatch.delenv("M5_TEST_FAIL_STAT_AT")
    native_manifest.manifest_test_reset()
    assert not _recover(native_manifest, final, temp)  # proven regression
    assert final.read_bytes() == final_before
    assert backup.read_bytes() == backup_before
    # The actual boot path must gate the WAV scan behind recovery.
    main = (ROOT / "firmware/main/main.c").read_text(encoding="utf-8")
    writer = main.split("static void recorder_writer_task(void *arg)", 1)[1]
    assert writer.index("recorder_ensure_manifest_exists()") < writer.index(
        "device_manifest_sync_wav_dir("
    )


@pytest.mark.parametrize("failure_occurrence", [1, 2])
def test_date_subtree_stat_eio_vetoes_index_scan_on_replay(
    native_manifest, tmp_path, monkeypatch, failure_occurrence
):
    final, temp = _create(tmp_path, [])
    recordings = tmp_path / "recordings"
    date_dir = recordings / "2026-10-13"
    date_dir.mkdir(parents=True)
    wav_data = _wav(b"\\0" * 64)
    wav_path = date_dir / Path(_entry(1)["filename"]).name
    wav_path.write_bytes(wav_data)
    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_STAT_MATCH", os.fspath(date_dir))
    monkeypatch.setenv("M5_TEST_FAIL_STAT_AT", str(failure_occurrence))
    if failure_occurrence == 2:
        # The first scan succeeds with exactly one stat of the date tree.
        assert _scan(native_manifest, recordings, final, temp) == (True, 1, 0)
        assert native_manifest.manifest_test_stat_match_calls() == 1
        assert _recover(native_manifest, final, temp)
    before = final.read_bytes()

    ok, added, excluded = _scan(native_manifest, recordings, final, temp)
    assert not ok and added == 0 and excluded == 0
    assert native_manifest.device_manifest_last_scan_error() == 2
    assert native_manifest.manifest_test_stat_match_calls() == failure_occurrence
    assert final.read_bytes() == before
    assert wav_path.read_bytes() == wav_data

    monkeypatch.delenv("M5_TEST_FAIL_STAT_MATCH")
    monkeypatch.delenv("M5_TEST_FAIL_STAT_AT")
    native_manifest.manifest_test_reset()
    assert _recover(native_manifest, final, temp)
    assert _scan(native_manifest, recordings, final, temp)[0]
    assert native_manifest.device_manifest_last_scan_indexed() == 1


def test_noncanonical_child_stat_eio_is_not_silent_exclusion(
    native_manifest, tmp_path, monkeypatch
):
    final, temp = _create(tmp_path, [])
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    legacy = recordings / "legacy"
    legacy.mkdir()
    native_manifest.manifest_test_reset()
    monkeypatch.setenv("M5_TEST_FAIL_STAT_MATCH", os.fspath(legacy))
    monkeypatch.setenv("M5_TEST_FAIL_STAT_AT", "1")
    before = final.read_bytes()
    assert not _scan(native_manifest, recordings, final, temp)[0]
    assert native_manifest.device_manifest_last_scan_error() == 2
    assert final.read_bytes() == before
    assert legacy.exists()

    monkeypatch.delenv("M5_TEST_FAIL_STAT_MATCH")
    monkeypatch.delenv("M5_TEST_FAIL_STAT_AT")
    native_manifest.manifest_test_reset()
    assert _scan(native_manifest, recordings, final, temp) == (True, 0, 0)
