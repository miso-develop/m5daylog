"""#147 FI2-R: execute the real production C read-only SD admission verifier.

Synthetic local files only; not live SD, actual custody, target identity,
Flash, an approved installation receipt, or physical/Windows acceptance.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

COMP = Path(__file__).resolve().parents[1] / "components" / "recorder"
DID = "11111111-1111-4111-8111-111111111111"
RID = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-31T12:00:00+00:00"
FILENAME = f"recordings/2026-10-31/120000_{RID}.wav"
PCM = b"\x02\x01" * 16000


@pytest.fixture(scope="module")
def read_only_binary(tmp_path_factory):
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("native C compiler unavailable")
    directory = tmp_path_factory.mktemp("settled_sd_native")
    harness = directory / "harness.c"
    harness.write_text(r"""
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "device_manifest.h"
int main(int argc, char **argv) {
    uint32_t count = 0;
    if (argc == 7 && strcmp(argv[1], "--scan") == 0) {
        uint32_t added = 0, skipped = 0;
        int ok = device_manifest_sync_wav_dir(
            argv[2], argv[3], argv[4], argv[5], argv[6], "recovered",
            &added, &skipped);
        printf("scanned=%d added=%u skipped=%u\n",
               ok, (unsigned)added, (unsigned)skipped);
        return 0;
    }
    if (argc == 7) {
        device_manifest_result_t result = device_manifest_upsert_file(
            argv[2], argv[3], argv[5],
            "2026-10-31T12:01:00+00:00", argv[6]);
        printf("append=%d\n", (int)result);
        return 0;
    }
    if (argc != 6) return 9;
    int ok = device_manifest_verify_settled_readonly(
        argv[1], argv[2], argv[3], argv[4], argv[5], &count);
    printf("admitted=%d count=%u\n", ok, (unsigned)count);
    return 0;
}
""")
    exe = directory / "read_only_admit"
    result = subprocess.run(
        [cc, "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-Wno-error=sign-compare",  # pre-existing sha256.c ternary warning
         "-I", str(COMP / "include"),
         str(COMP / "sha256.c"), str(COMP / "device_identity.c"),
         str(COMP / "device_manifest.c"), str(harness), "-o", str(exe)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return exe


def wav_bytes(pcm=PCM):
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm),
        b"WAVE", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16,
        b"data", len(pcm),
    ) + pcm


def canonical_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def inventory(root):
    return {
        str(f.relative_to(root)): f.read_bytes()
        for f in root.rglob("*") if f.is_file()
    }


def fixture_tree(root):
    base = root / "M5DAYLOG"
    date = base / "recordings" / "2026-10-31"
    date.mkdir(parents=True)
    wav = date / f"120000_{RID}.wav"
    audio = wav_bytes()
    wav.write_bytes(audio)
    (base / "device.json").write_text(canonical_json({
        "schemaVersion": 1, "deviceId": DID,
        "model": "M5Capsule v1.1", "firmwareVersion": "0.1.0",
        "audioCapabilities": {
            "sampleRate": 16000, "bitDepth": 16,
            "channels": 1, "format": "pcm"
        },
    }))
    entry = {
        "recordingId": RID, "filename": FILENAME,
        "startedAt": STAMP, "durationMs": 1000,
        "sizeBytes": len(audio), "sha256": hashlib.sha256(audio).hexdigest(),
        "sampleRate": 16000, "bitDepth": 16, "channels": 1,
        "state": "finalized", "firmwareVersion": "0.1.0",
    }
    (base / "manifest.json").write_text(canonical_json({
        "schemaVersion": 1, "deviceId": DID,
        "updatedAt": STAMP, "recordings": [entry],
    }))
    return base, wav


def admit(exe, base, did=DID):
    result = subprocess.run(
        [str(exe), str(base / "recordings"),
         str(base / "manifest.json"), str(base / "manifest.tmp"),
         str(base / "device.json"), did],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return dict(part.split("=", 1) for part in result.stdout.strip().split())


def test_authorized_postgrant_normal_index_scan_is_noop_for_settled_r(
    read_only_binary, tmp_path
):
    # Production C indexer is also reachable in main.c's normal recorder boot
    # after a writable SD mount. This is a *postgrant* observation only:
    # external grant consumption, selected APP and physical custody remain
    # unverified. A settled original R card must not be silently reindexed.
    base, original_wav = fixture_tree(tmp_path)
    original = original_wav.read_bytes()
    historical = json.loads((base / "manifest.json").read_text())["recordings"]
    assert admit(read_only_binary, base) == {"admitted": "1", "count": "1"}
    before = inventory(tmp_path)
    run = subprocess.run(
        [str(read_only_binary), "--scan", str(base / "recordings"),
         str(base / "manifest.json"), str(base / "manifest.tmp"),
         DID, STAMP], capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    assert run.stdout.strip() == "scanned=1 added=0 skipped=0"
    assert inventory(tmp_path) == before
    assert original_wav.read_bytes() == original
    assert json.loads((base / "manifest.json").read_text())["recordings"] == historical


def test_complete_settled_r_is_read_only(read_only_binary, tmp_path):
    base, _ = fixture_tree(tmp_path)
    before = inventory(tmp_path)
    assert admit(read_only_binary, base) == {"admitted": "1", "count": "1"}
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("case", [
    "manifest_missing", "manifest_corrupt", "manifest_tmp",
    "manifest_bak", "device_missing", "device_mismatch",
    "device_extra", "manifest_device_mismatch", "wav_missing",
    "wav_truncated", "wav_corrupt_same_size", "wav_orphan",
    "part_pending", "nested_temp", "unknown_date", "unknown_root_file",
    "empty_r", "manifest_hash_mismatch", "manifest_size_mismatch",
    "quarantine_pending", "unrecognized_root_tmp", "empty_quarantine_control",
    "manifest_duplicate", "id_mismatch", "timestamp_invalid",
])
def test_r_rejects_unsettled_or_conflicting_media_without_writing(
    read_only_binary, tmp_path, case
):
    base, wav = fixture_tree(tmp_path)
    manifest = base / "manifest.json"
    doc = json.loads(manifest.read_text())
    if case == "manifest_missing":
        manifest.unlink()
    elif case == "manifest_corrupt":
        manifest.write_text("{")
    elif case == "manifest_tmp":
        (base / "manifest.tmp").write_text("M1")
    elif case == "manifest_bak":
        (base / "manifest.json.bak").write_text("M0")
    elif case == "device_missing":
        (base / "device.json").unlink()
    elif case == "device_mismatch":
        d = json.loads((base / "device.json").read_text())
        d["deviceId"] = RID
        (base / "device.json").write_text(canonical_json(d))
    elif case == "device_extra":
        (base / "device.json").write_text((base / "device.json").read_text() + " ")
    elif case == "manifest_device_mismatch":
        doc["deviceId"] = RID
    elif case == "wav_missing":
        wav.unlink()
    elif case == "wav_truncated":
        wav.write_bytes(wav.read_bytes()[:-2])
    elif case == "wav_corrupt_same_size":
        data = bytearray(wav.read_bytes())
        data[-1] ^= 3
        wav.write_bytes(data)
    elif case == "wav_orphan":
        (wav.parent / f"120100_{DID}.wav").write_bytes(wav_bytes())
    elif case == "part_pending":
        (wav.parent / f"120100_{DID}.wav.part").write_bytes(b"partial")
    elif case == "nested_temp":
        (wav.parent / "temp.tmp").write_bytes(b"test")
    elif case == "unknown_date":
        (base / "recordings" / "2026-11-31").mkdir()
    elif case == "unknown_root_file":
        (base / "recordings" / "mystery.bin").write_bytes(b"test")
    elif case == "quarantine_pending":
        quarantine = base / "quarantine"
        quarantine.mkdir()
        (quarantine / "orphan.wav.part").write_bytes(b"unindexed")
    elif case == "unrecognized_root_tmp":
        (base / "device.json.tmp").write_text("uncertain")
    elif case == "empty_quarantine_control":
        (base / "quarantine").mkdir()
        (base / "quarantine" / "index.tmp").write_bytes(b"unknown")
    elif case == "empty_r":
        doc["recordings"] = []
        wav.unlink()
    elif case == "manifest_hash_mismatch":
        doc["recordings"][0]["sha256"] = "f" * 64
    elif case == "manifest_size_mismatch":
        doc["recordings"][0]["sizeBytes"] += 2
        doc["recordings"][0]["durationMs"] += 1
    elif case == "manifest_duplicate":
        doc["recordings"].append(dict(doc["recordings"][0]))
    elif case == "id_mismatch":
        doc["recordings"][0]["recordingId"] = DID
    elif case == "timestamp_invalid":
        doc["recordings"][0]["startedAt"] = "2026-99-99T12:00:00+00:00"
    if case in {
        "manifest_device_mismatch", "empty_r", "manifest_hash_mismatch",
        "manifest_size_mismatch", "manifest_duplicate",
        "id_mismatch", "timestamp_invalid"
    }:
        manifest.write_text(canonical_json(doc))
    before = inventory(tmp_path)
    assert admit(read_only_binary, base)["admitted"] == "0"
    assert inventory(tmp_path) == before, "admission must never mutate SD"


def test_expected_identity_is_not_inferred_from_sd(read_only_binary, tmp_path):
    base, _ = fixture_tree(tmp_path)
    before = inventory(tmp_path)
    assert admit(read_only_binary, base, RID)["admitted"] == "0"
    assert inventory(tmp_path) == before


def test_no_implicit_normal_boot_or_recovery_calls():
    src = (COMP / "device_manifest.c").read_text()
    section = src.split("bool device_manifest_verify_settled_readonly(", 1)[1]
    for write in ("fopen(", "rename(", "remove(", "unlink(",
                  "device_manifest_upsert_file(", "device_manifest_recover_tmp(",
                  "device_manifest_sync_wav_dir(", "wav_recovery_scan_recordings("):
        assert write not in section
    assert "device_manifest_verify_settled_readonly(" not in (
        Path(__file__).resolve().parents[1] / "main" / "main.c"
    ).read_text()



def test_later_authorized_append_preserves_historical_wav_and_entry(
    read_only_binary, tmp_path
):
    # The admission scan is read-only. This separate production upsert
    # simulates only a later independently authorized new recording, not
    # installer admission, release, or actual flash/SD ownership proof.
    base, previous_wav = fixture_tree(tmp_path)
    previous_manifest = json.loads((base / "manifest.json").read_text())
    before_original_audio = previous_wav.read_bytes()
    before_device = (base / "device.json").read_bytes()
    new_id = "33333333-3333-4333-8333-333333333333"
    new_name = f"recordings/2026-10-31/120100_{new_id}.wav"
    new_wav = base / new_name
    new_audio = wav_bytes(b"\x03\x02" * 16000)
    new_wav.write_bytes(new_audio)
    new_entry = canonical_json({
        "recordingId": new_id, "filename": new_name,
        "startedAt": "2026-10-31T12:01:00+00:00",
        "durationMs": 1000, "sizeBytes": len(new_audio),
        "sha256": hashlib.sha256(new_audio).hexdigest(),
        "sampleRate": 16000, "bitDepth": 16, "channels": 1,
        "state": "finalized", "firmwareVersion": "0.1.0",
    })
    command = [
        str(read_only_binary), str(base / "recordings"),
        str(base / "manifest.json"), str(base / "manifest.tmp"),
        str(base / "device.json"), DID, new_entry,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "append=0" in result.stdout
    after = json.loads((base / "manifest.json").read_text())
    assert after["recordings"][:1] == previous_manifest["recordings"]
    assert len(after["recordings"]) == 2
    assert previous_wav.read_bytes() == before_original_audio
    assert (base / "device.json").read_bytes() == before_device
    assert admit(read_only_binary, base) == {"admitted": "1", "count": "2"}
