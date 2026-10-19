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

**Independent historical evidence (ENG-01 correction):** In addition to the maintenance receipt, the preflight requires three local evidence artifacts whose SHA-256 pins MUST be copied from an independent, already-approved private operator/Human Gate record: the original consumed clear journal, the original preserved backup/manifest evidence, and a separate signed-off historical attestation. The attestation is a UTF-8 JSON object with exactly these keys: `schema_version=1`, `issue=119`, `device_id`, `receipt_sha256`, `clear_journal_sha256`, `preservation_sha256`, `clear_guard_consumed=true`, `verified_manifest_wavs=76`, `verified_backups=117`, `verified_protected=38`, and `deleted` equal to the four historical deletion counts. Its attested claims MUST be verified against the **original private journal/preservation evidence by an independent operator before its hash is approved**. The old journal and preservation documents are treated as opaque immutable evidence; merely matching their hashes does not independently validate their semantics. The tool cannot sign, prepare or authorize this attestation. If independent original-history verification/pins are unavailable, `ORIGINAL_CLEAR_EVIDENCE_REQUIRED` or a historical mismatch is `BLOCKED`—never synthesize a new history to make the check pass.

`backups` requires **exactly 117** distinct host-backup file entries. `protected` requires **exactly 38** distinct retained-Device file entries (including `M5DAYLOG/device.json`). Both are fully rehashed, not just counted. The recorded deletion counts, one-shot clear guard, and Device metadata must match. All currently visible media files must equal the protected inventory exactly. Unexpected files, directories, recordings, recovered/quarantined items, part files, manifest/events reappearance, unsafe symbolic links, missing/different files or unqualified target all block. The cleared `M5DAYLOG/recordings` directory must exist and be empty. Existing acknowledged protected files, if any, are included only by the exact pinned protected index. The CLI does not generate or change this receipt.

**SEC-01 filesystem boundary:** The nominated host backup root, Device media root, retained private evidence paths, and the one-shot guard location must have ordinary directories along their entire ancestor chains. Windows `FILE_ATTRIBUTE_REPARSE_POINT` / reparse-tagged objects (including NTFS directory junctions and volume mount points), POSIX symlinks, and reparse-tagged files are rejected before a file is hashed or an inventory subtree is entered. Backup file paths are also resolved relative to the operator-nominated authorized host backup root before and after hashing; a matching hash and distinct file ID outside that root is never enough. A redirected backup root, descendant, protected Device path, or retained historical-evidence path is `BLOCKED`, not `PREFLIGHT_ONLY`. The root's **authorization and historical authenticity** still depend on independently approved private evidence and the Human Gate; pathname resolution alone does not provide that approval.

These checks are read-only, fail-closed preflight defenses against existing unsafe links and detectable swaps—not an attested atomic Windows snapshot against an adversarial concurrent directory replacement. Windows NTFS junction and power-loss behavior require separate native qualification; neither portable tests nor a valid preflight enable a physical release. The production locked-snapshot gate remains intentionally unavailable.

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
  --history-attestation <private-approved-attestation-json>
  --history-sha256 <independently-pinned-attestation-digest>
  --clear-journal <original-immutable-clear-journal>
  --clear-journal-sha256 <independently-pinned-clear-digest>
  --preservation-receipt <original-immutable-preservation-evidence>
  --preservation-sha256 <independently-pinned-preservation-digest>
```

The command line above is a reference for the **preflight-only** program, not a direction to run against connected hardware without permission. It outputs no host paths, Device IDs, raw audio, receipt contents or private file names. The command has **no `--execute` option**. It validates Device metadata, qualified filesystem inventory, backup/protected hashes, exactly one qualified target volume/disk and correlated CDC identity; additionally the tool rejects any other removable USB-storage disk candidate (including unrelated removable USB drives) rather than risk accepting a second qualified M5Daylog using pre-existing read-only host APIs. It does not prove firmware runtime identity, Device release capability, Device SD ownership or a separate one-shot attempt approval.

Software tests (host-only, synthetic):

```text
cd pc
PYTHONPATH=src python -m unittest discover -s tests -v
```

## Future Human Gate boundary (not authorized here)

**Rework safety boundaries:** A single canonical issue-119 host-only guard filename is derived within a separately approved, immutable guard directory returned by the trusted per-attempt permit. The permit also binds the detached historical-attestation SHA-256; supplying a different guard directory is denied. A second directory cannot be self-approved by ordinary operator CLI, which has no execution path. Before consuming the guard or sending any CDC byte, the coordinator requires an independently qualified `prove_locked_snapshot(target, preflight)` capability on the release adapter **while the exact volume lock remains held**. The merged #89 `WindowsHostReleasePort` does not provide this attested locked-media freshness capability; therefore real release is deliberately `BLOCKED` until a separate reviewed native implementation/Human Gate proves absence of new writes between inventory and lock. The synthetic FakePort simulates this condition but cannot certify a Windows/FAT32 volume.

The `MaintenanceReleaseCoordinator` requires an externally trusted, independently reviewed **Human Gate authorization/qualification callback** returning a `GatePermit` for the exact verified receipt, frozen target and unique release attempt, including running handler evidence. The `GatePermit` dataclass is not itself trusted authorization. The coordinator then re-preflights, freezes/revalidates the exact host target, seals ordinary I/O, acquires the dedicated release lease, retains exact volume lock+dismount, consumes a host-only one-shot guard **before** any CDC command, and uses the existing #89 release port and volume-disappearance confirmation. Exception/timeout/replay blocks; successful outcome is only `MAINTENANCE_RELEASE_ONLY`.

Before enabling physical release, independent reviewers and Integration must verify Windows host journal durability (including directory-entry persistence and crash behavior), exact Device/host baseline, real handler provenance, one-shot guard placement **outside the mounted Device**, and safe bounded post-command observation. The current portable `OneShotGuard` test is not proof of Windows power-loss persistence. A separate exact-attempt operator approval and Human Gate plan is mandatory. If this cannot be qualified, no native execute path is to be enabled and the Device stays PC-owned/HOST_UNRESOLVED.

#119 physical cycles are unchanged (zero). PR #120 remains Draft. #54 EV-018 remains blocked.
