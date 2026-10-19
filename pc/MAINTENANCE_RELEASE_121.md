# #121 — Post-clear maintenance release (software staging only)

Authority: [D-031 amendment](https://github.com/miso-develop/m5daylog/issues/105#issuecomment-6267701110) and [S-004 amendment](https://github.com/miso-develop/m5daylog/issues/37#issuecomment-6267717379). Not an amendment to ordinary `sync` or `sync-release`.

## Status and safety boundary

The only executable CLI for this change is a **read-only preflight**. It does **not** lock/dismount a volume, open a CDC command endpoint, send `RELEASE_STORAGE`, reset/flash, alter NVS, delete media, or invoke a physical acceptance action. Its success output is **PREFLIGHT_ONLY**, not an authorization or physical Human Gate PASS. No approved native execution adapter is present in this task. The injected execution coordinator exists for fail-closed unit tests and future independently reviewed Human Gate integration.

When current **running** firmware/handler provenance is unknown, as in #119's existing image/hash chronology, release remains **BLOCKED_PROVENANCE** regardless of a passing backup/media inventory. There is no automatic ROM/readback/reset fallback.

## Receipt format: private local evidence, not a self-signed authority

The operator must independently preserve and pin the exact SHA-256 of a UTF-8 JSON receipt in an approved private evidence record. **A digest supplied from the same mutable receipt is not independent authentication**. The CLI cannot attest the operator's approval or running firmware.

The receipt schema is a locally prepared, intentionally narrow snapshot:

```json
{
  "schema_version": 1,
  "issue": 119,
  "device_id": "<canonical-deviceId-from-validated-device.json>",
  "clear_guard_consumed": true,
  "deleted": {
    "wav": 76,
    "manifests": 2,
    "events": 1,
    "recording_directories": 10
  },
  "backups": [
    {"path": "backup-relative-file-name", "size": 123, "sha256": "<64-lowercase-hex>"}
  ],
  "protected": [
    {"path": "M5DAYLOG/device.json", "size": 123, "sha256": "<64-lowercase-hex>"}
  ]
}
```

`backups` requires **exactly 117** distinct host-backup file entries. `protected` requires **exactly 38** distinct retained-Device file entries (including `M5DAYLOG/device.json`). Both are fully rehashed, not just counted. The recorded deletion counts, one-shot clear guard, and Device metadata must match. All currently visible media files must equal the protected inventory exactly. Unexpected files, directories, recordings, recovered/quarantined items, part files, manifest/events reappearance, unsafe symbolic links, missing/different files or unqualified target all block. The cleared `M5DAYLOG/recordings` directory must exist and be empty. Existing acknowledged protected files, if any, are included only by the exact pinned protected index. The CLI does not generate or change this receipt.

These historical counts describe **one specifically authorized media clear**, not general release policy. If the retained inventory differs from this strict interpretation, stop and reconcile the original preserved evidence; do not edit the receipt merely to make preflight pass.

## Preflight-only CLI

After review and separate approval for *host read-only* observation, on Windows 10 with Python 3.11 and the source checkout:

```text
python -m m5daylog.maintenance_release
  --device-root <qualified-drive-root>
  --backup-root <host-private-backup-root>
  --receipt <host-private-receipt-json>
  --receipt-sha256 <externally-pinned-sha256>
  --cdc-port <qualified-com-port>
  --contracts-dir <repo-contracts-dir>
```

The command line above is a reference for the **preflight-only** program, not a direction to run against connected hardware without permission. It outputs no host paths, Device IDs, raw audio, receipt contents or private file names. The command has **no `--execute` option**. It validates Device metadata, qualified filesystem inventory, backup/protected hashes, exactly one target volume/disk and correlated CDC identity using pre-existing read-only host APIs. It does not prove firmware runtime identity, Device release capability, Device SD ownership or a separate one-shot attempt approval.

Software tests (host-only, synthetic):

```text
cd pc
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Future Human Gate boundary (not authorized here)

The `MaintenanceReleaseCoordinator` requires an externally trusted, independently reviewed **Human Gate authorization/qualification callback** returning a `GatePermit` for the exact verified receipt, frozen target and unique release attempt, including running handler evidence. The `GatePermit` dataclass is not itself trusted authorization. The coordinator then re-preflights, freezes/revalidates the exact host target, seals ordinary I/O, acquires the dedicated release lease, retains exact volume lock+dismount, consumes a host-only one-shot guard **before** any CDC command, and uses the existing #89 release port and volume-disappearance confirmation. Exception/timeout/replay blocks; successful outcome is only `MAINTENANCE_RELEASE_ONLY`.

Before enabling physical release, independent reviewers and Integration must verify Windows host journal durability (including directory-entry persistence and crash behavior), exact Device/host baseline, real handler provenance, one-shot guard placement **outside the mounted Device**, and safe bounded post-command observation. The current portable `OneShotGuard` test is not proof of Windows power-loss persistence. A separate exact-attempt operator approval and Human Gate plan is mandatory. If this cannot be qualified, no native execute path is to be enabled and the Device stays PC-owned/HOST_UNRESOLVED.

#119 physical cycles are unchanged (zero). PR #120 remains Draft. #54 EV-018 remains blocked.
