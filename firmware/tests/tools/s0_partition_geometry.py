#!/usr/bin/env python3
"""#113 S0: offline, fail-closed ESP-IDF partition geometry evidence.

Reads build output from an EXACT pinned, clean ESP-IDF build of PR #109.
Creates a hypothetical candidate binary with the SAME pinned generator.
No hardware, flash, NVS, or MSC access. No production source modification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys

FLASH_END = 0x800000
SECTOR = 0x1000
TABLE_OFFSET = 0x8000
EXISTING = {
    "nvs": (0x01, 0x02, 0x9000, 0x6000),
    "phy_init": (0x01, 0x01, 0xF000, 0x1000),
    "factory": (0x00, 0x00, 0x10000, 0x100000),
}
ADDED = ("ack_state", 0x01, 0x02, 0x780000, 0x80000)
CSV_SOURCE = (
    "# Name,Type,SubType,Offset,Size\n"
    "nvs,data,nvs,0x9000,0x6000\n"
    "phy_init,data,phy,0xF000,0x1000\n"
    "factory,app,factory,0x10000,0x100000\n"
    "ack_state,data,nvs,0x780000,0x80000\n"
)
REFERENCE_HEAD = "f69ec03603f657af9c5dc8eca7ae8f95e42d66fb"
IMAGE_SHA = "sha256:6e2800a69f1c6521a5651da524f811e237d13e34cad369687916d0ad0bc4ef89"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hexi(value: int) -> str:
    return f"0x{value:X}"


def assertion(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def partition_entries(binary: bytes) -> list[dict]:
    assertion(len(binary) <= SECTOR, "partition binary spans more than one sector")
    assertion(len(binary) >= 32 and len(binary) % 32 == 0, "invalid table length")
    entries = []
    saw_md5 = False
    for i in range(0, len(binary), 32):
        chunk = binary[i:i + 32]
        if chunk == b"\xff" * 32:
            break
        magic = struct.unpack_from("<H", chunk)[0]
        if magic == 0xEBEB:
            saw_md5 = True
            break
        assertion(magic == 0x50AA, f"unrecognized partition entry magic at {i}")
        _, typ, subtype, offset, size, raw_name, flags = struct.unpack("<HBBII16sI", chunk)
        raw_name = raw_name.split(b"\0", 1)[0]
        try:
            name = raw_name.decode("ascii")
        except UnicodeDecodeError as ex:
            raise ValueError("non-ASCII partition name") from ex
        assertion(bool(name), "empty partition name")
        entries.append({
            "name": name, "type": typ, "subtype": subtype,
            "offset": offset, "size": size, "end": offset + size, "flags": flags,
        })
    assertion(saw_md5, "partition table lacks a generated MD5 record")
    assertion(len({p["name"] for p in entries}) == len(entries), "duplicate partition label")
    return entries


def validate_geometry(entries: list[dict], candidate: bool) -> None:
    expected_count = 4 if candidate else 3
    assertion(len(entries) == expected_count, f"expected {expected_count} entries, found {len(entries)}")
    observed = {p["name"]: p for p in entries}
    for name, signature in EXISTING.items():
        p = observed.get(name)
        assertion(p is not None, f"missing existing entry {name}")
        assertion(
            (p["type"], p["subtype"], p["offset"], p["size"]) == signature,
            f"protected partition mismatch: {name}",
        )
        assertion(p["flags"] == 0, f"unexpected flags on {name}")
    if candidate:
        name, typ, sub, offset, size = ADDED
        p = observed.get(name)
        assertion(p is not None, "candidate ack_state missing")
        assertion(
            (p["type"], p["subtype"], p["offset"], p["size"]) == (typ, sub, offset, size),
            "ack_state shifted, resized or changed subtype",
        )
        assertion(p["flags"] == 0, "ack_state flags unexpectedly set")
        assertion(p["end"] == FLASH_END, "ack_state does not end at 8MiB")
    assertion(set(observed) == (set(EXISTING) | ({"ack_state"} if candidate else set())), "unexpected partition")
    for p in entries:
        assertion(p["offset"] % SECTOR == 0, f"not sector-aligned: {p['name']}")
        assertion(p["size"] > 0 and p["size"] % SECTOR == 0, f"size not sector-aligned: {p['name']}")
        assertion(p["end"] <= FLASH_END and p["offset"] >= TABLE_OFFSET + SECTOR,
                  f"out of range: {p['name']}")
    ordered = sorted(entries, key=lambda p: p["offset"])
    for a, b in zip(ordered, ordered[1:]):
        assertion(a["end"] <= b["offset"], f"overlapping partitions: {a['name']}/{b['name']}")


def sdkconfig_controls(path: Path) -> dict:
    controls = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("CONFIG_") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"CONFIG_IDF_TARGET", "CONFIG_PARTITION_TABLE_OFFSET",
                   "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME", "CONFIG_BOOTLOADER_OFFSET_IN_FLASH",
                   "CONFIG_ESPTOOLPY_FLASHMODE", "CONFIG_ESPTOOLPY_FLASHFREQ"} or \
                key.startswith("CONFIG_PARTITION_TABLE_") or key.startswith("CONFIG_ESPTOOLPY_FLASHSIZE_"):
            controls[key] = value
    assertion(controls.get("CONFIG_IDF_TARGET") == '"esp32s3"', "wrong SoC")
    assertion(controls.get("CONFIG_PARTITION_TABLE_SINGLE_APP") == "y", "not built-in single-app table")
    assertion(controls.get("CONFIG_PARTITION_TABLE_OFFSET") == "0x8000", "partition table offset drifted")
    assertion(controls.get("CONFIG_ESPTOOLPY_FLASHSIZE_8MB") == "y", "flash size not 8MiB")
    assertion(controls.get("CONFIG_PARTITION_TABLE_CUSTOM") != "y", "source has custom partition table")
    return controls


def read_flasher_args(build: Path) -> tuple[dict, list[dict]]:
    obj = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    files = obj.get("flash_files")
    assertion(isinstance(files, dict) and len(files) == 3, "unexpected old image flash set")
    output = []
    for offset_text, rel_name in files.items():
        offset = int(offset_text, 0)
        root = build.resolve()
        path = (build / rel_name).resolve()
        assertion(path.is_relative_to(root) and path.is_file(), "unsafe/missing old build output")
        raw_size = path.stat().st_size
        padded_size = (raw_size + SECTOR - 1) // SECTOR * SECTOR
        output.append({
            "offset": offset, "length": raw_size, "sector_write_envelope": padded_size,
            "end": offset + padded_size, "sha256": digest(path),
            "relative_path": path.relative_to(root).as_posix(),
        })
    output.sort(key=lambda e: e["offset"])
    assertion([e["offset"] for e in output] == [0x0, 0x8000, 0x10000],
              "unexpected old flash offsets (bootloader/table/app)")
    assertion(output[0]["end"] <= TABLE_OFFSET, "bootloader overlaps partition-table sector")
    assertion(output[1]["end"] <= 0x9000, "partition table exceeds reserved sector")
    assertion(output[2]["end"] <= 0x110000, "factory image exceeds 1MiB app envelope")
    assertion(output[2]["length"] > 0, "empty factory image")
    for a, b in zip(output, output[1:]):
        assertion(a["end"] <= b["offset"], "old flash images overlap")
    flash_settings = obj.get("flash_settings", {})
    assertion(flash_settings.get("flash_size") in ("8MB", "8mb"), "flasher_args flash size not 8MB")
    return flash_settings, output


def evidence(args: argparse.Namespace) -> dict:
    build = args.baseline_root / "firmware" / "build"
    assertion(args.baseline_root.is_dir(), "pinned old source directory missing")
    config = args.baseline_root / "firmware" / "sdkconfig"
    old_binary = build / "partition_table" / "partition-table.bin"
    generator = args.idf_path / "components" / "partition_table" / "gen_esp32part.py"
    for path in (config, old_binary, generator):
        assertion(path.is_file(), f"required generated/toolchain file absent: {path.name}")

    # Decode both binary tables using the pinned IDF generator; independently
    # parse the raw entries so a pretty printed CSV cannot silently hide drift.
    out = args.output
    candidate_source = out / "candidate.csv"
    candidate_binary = out / "candidate.bin"
    candidate_source.write_text(CSV_SOURCE, encoding="ascii")
    subprocess.run([sys.executable, str(generator), str(candidate_source), str(candidate_binary)],
                   check=True, capture_output=True, text=True)
    for source, target in ((old_binary, out / "old-decoded.csv"),
                           (candidate_binary, out / "candidate-decoded.csv")):
        subprocess.run([sys.executable, str(generator), str(source), str(target)],
                       check=True, capture_output=True, text=True)

    old_entries = partition_entries(old_binary.read_bytes())
    new_entries = partition_entries(candidate_binary.read_bytes())
    validate_geometry(old_entries, candidate=False)
    validate_geometry(new_entries, candidate=True)
    assertion(new_entries[:3] == old_entries, "old entries changed in candidate binary")
    assertion(old_binary.read_bytes() != candidate_binary.read_bytes(),
              "candidate binary unexpectedly equals old binary")
    controls = sdkconfig_controls(config)
    flash_settings, old_images = read_flasher_args(build)
    assertion(old_images[1]["sha256"] == digest(old_binary), "flasher table image differs from generated table")
    candidate_padded = out / "candidate-sector.bin"
    candidate_data = candidate_binary.read_bytes()
    assertion(len(candidate_data) <= SECTOR, "candidate table does not fit sector")
    candidate_padded.write_bytes(candidate_data.ljust(SECTOR, b"\xff"))

    # These are phase-ordered potential writes, NOT a flash/provisioning plan.
    # No new reader/bridge/provisioning image exists. An S0 PASS cannot be
    # fabricated by treating the old default-NVS-only app as the new app.
    write_set = [
        {"phase": "keep", "name": "existing bootloader", "offset": hexi(old_images[0]["offset"]),
         "write_length": 0, "reference_sha256": old_images[0]["sha256"],
         "status": "unchanged assumption; future image association not authorized"},
        {"phase": "PREPARING", "name": "Device-owned default NVS anchor", "offset": "0x9000..0xF000",
         "write_length": None, "sha256": None, "status": "S1 mechanism/sector-write-set unknown; cannot image or erase"},
        {"phase": "bridge", "name": "old-table-compatible bridge application", "offset": "0x10000",
         "write_length": None, "sha256": None, "status": "missing compatible bridge image and hash"},
        {"phase": "transition", "name": "candidate partition table (one whole sector)",
         "offset": "0x8000", "write_length": SECTOR, "sha256": digest(candidate_padded),
         "status": "static candidate only, not authorized for flash"},
        {"phase": "transition", "name": "new dual-reader factory application",
         "offset": "0x10000", "max_write_length": 0x100000,
         "write_length": None, "sha256": None, "status": "new compatible application absent; old app is forbidden after ACTIVE"},
        {"phase": "provision", "name": "ack_state internal NVS", "offset": "0x780000",
         "max_write_length": 0x80000, "write_length": None, "sha256": None,
         "status": "no initializer/header generation and no proof existing high-flash bytes are disposable"},
        {"phase": "ACTIVE", "name": "existing default NVS ACTIVE anchor", "offset": "0x9000..0xF000",
         "write_length": None, "sha256": None, "status": "S1 commit/GC write-set unresolved"},
    ]
    return {
        "schema": "m5daylog.s0.offline_geometry.v1",
        "reference_device_pr_head": REFERENCE_HEAD,
        "toolchain": {"esp_idf": "v5.5.5", "container_digest": IMAGE_SHA,
                      "generator_sha256": digest(generator)},
        "baseline": {"sdkconfig_sha256": digest(config), "sdkconfig_controls": controls,
                     "tracked_sdkconfig_defaults_sha256":
                         digest(args.baseline_root / "firmware" / "sdkconfig.defaults"),
                     "flasher_args_sha256": digest(build / "flasher_args.json"),
                     "flash_settings": flash_settings, "flash_images": old_images,
                     "old_partition_binary_sha256": digest(old_binary),
                     "old_partition_binary_size": old_binary.stat().st_size,
                     "old_partition_entries": old_entries},
        "candidate": {"csv_sha256": digest(candidate_source),
                      "partition_binary_sha256": digest(candidate_binary),
                      "partition_binary_size": candidate_binary.stat().st_size,
                      "partition_sector_sha256": digest(candidate_padded),
                      "partition_entries": new_entries,
                      "unallocated_between_factory_and_ack": "0x110000..0x780000"},
        "planned_write_set": write_set,
        "read_only_limits": [
            "generated old table is NOT the installed device partition table",
            "0x780000..0x800000 may contain irreplaceable physical target data",
            "bridge/new-app/new-header complete byte images and hashes do not yet exist",
            "actual NVS committed history, headroom, garbage collection, flash wear and boot/heap unavailable",
            "non-atomic sector transitions/power-interruption and S1/S2/S3 Q3 safety not established",
            "no flashing, MSC, hardware, device ID, raw recording or NVS fixture was accessed",
        ],
        "geometry_static_result": "PASS",
        "full_s0_admission_result": "NO_GO_UNTIL_IMAGE_MANIFEST_AND_PHYSICAL_BASELINE",
        "physical_human_gate_s5": "NOT_AUTHORIZED_NOT_RUN",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--idf-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = args.output / "s0-report.json"
    try:
        result = evidence(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as exc:
        result = {
            "schema": "m5daylog.s0.offline_geometry.v1",
            "reference_device_pr_head": REFERENCE_HEAD,
            "geometry_static_result": "NO_GO",
            "full_s0_admission_result": "NO_GO",
            "error_type": type(exc).__name__, "error": str(exc)[:500],
            "physical_human_gate_s5": "NOT_AUTHORIZED_NOT_RUN",
        }
        report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("S0 GEOMETRY NO_GO: " + str(exc), file=sys.stderr)
        return 1
    report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("S0 GEOMETRY: " + result["geometry_static_result"])
    print("S0 FULL ADMISSION: " + result["full_s0_admission_result"])
    print("REPORT_SHA256: " + digest(report))
    print("OLD_TABLE_SHA256: " + result["baseline"]["old_partition_binary_sha256"])
    print("CANDIDATE_TABLE_SHA256: " + result["candidate"]["partition_binary_sha256"])
    print("CANDIDATE_SECTOR_SHA256: " + result["candidate"]["partition_sector_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
