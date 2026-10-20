# #121 — Post-clear maintenance release (software staging only)

Authority: [Human-approved D-031 controlled-PoC addendum](https://github.com/miso-develop/m5daylog/issues/105#issuecomment-6289059954) and [S-004 PoC addendum](https://github.com/miso-develop/m5daylog/issues/37#issuecomment-6289068527); historical [maintenance exception](https://github.com/miso-develop/m5daylog/issues/105#issuecomment-6267701110) remains in force except for explicitly deferred advanced proof. Not an amendment to ordinary `sync` or `sync-release`.

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

These checks are read-only, fail-closed preflight defenses against existing unsafe links and detectable swaps—not an attested atomic Windows snapshot against an adversarial concurrent directory replacement. The controlled one-operator/one-Device PoC assumes no known competing writer and requires a fresh recheck immediately before the host seal, plus actual same-target Windows LOCK/DISMOUNT under independent Human Gate control. Complete historical write-then-revert detection, raw FAT32 post-lock forensic proof, and adversarial namespace/actor exclusion are **DEFERRED_NOT_REQUIRED_FOR_CONTROLLED_POC / NOT_PROVEN**. No adapter in this PR can assert real operating-system or Device readiness.

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

**Rework safety boundaries:** A single canonical issue-119 host-only guard filename is derived within a separately approved, immutable guard directory returned by the trusted per-attempt permit. The permit also binds the detached historical-attestation SHA-256; supplying a different guard directory is denied. A second directory cannot be self-approved by ordinary operator CLI, which has no execution path. For the **supervised #119 PoC only**, the earlier mandatory `prove_locked_snapshot(target, preflight)` historical witness has been replaced in the *source-only coordinator* with `trusted_controlled_poc_qualifier`. That is a separately supplied Human Gate callback evaluated **inside the retained same-target lock/dismount context**, after the existing historical receipt and running-firmware `GatePermit` checks. It must return the exact typed `ControlledPocReadiness(mode="CONTROLLED_POC_MAINTENANCE_119", target, receipt_sha256, history_sha256, attempt_id, no_known_competing_writer=True, device_pc_owned=True, same_target_lock_dismounted=True)` for the current attempt. Missing callback, plain bool, wrong mode, drifted identity/receipt/attempt, ambiguous Device ownership, unqualified lock or known competing writer **BLOCKS before guard consumption/CDC**. This class is **only a typed carrier of independently established facts, not evidence by construction**, and the repository supplies **no real physical qualifier/adapter** to create one. There is no shortcut by reporting a mock locked snapshot or substituting final-byte equality for past write-history.

The host-local guard is a fixed, operator-approved path outside Device media, created exclusively and file-flushed before transmission, with exact-content readback and file-identity checking. Its record also labels the controlled-PoC scope. A pre-existing, partial, ambiguous or mismatched record blocks; uncertain writes may leave it consumed and **never justify retry**. A successful file `fsync` and readback **do not certify a newly created directory entry's survival through abrupt host power loss or hardware cache loss**. The PoC only claims at-most-one *attempt in the supervised controlled session*, not globally guaranteed exactly-once under host power failure. Crash, reboot, partial CDC send, lost ACK or ambiguous result => `BLOCKED` / `HOST_UNRESOLVED`, not a second attempt or automatically recreated journal. Any later recovery needs a fresh independently approved baseline and human decision.

The `MaintenanceReleaseCoordinator` remains an injected **test/Human-Gate-disabled design seam**, not reachable from the read-only CLI. It requires independently qualified running D-031 firmware, immutable historical 117/117 backup and 38/38 protected-file evidence, original clear journal, exact Device/MSC/CDC target, a re-preflight with no drift, safe MSC/CDC seal and zero owned handles, retained same-target Windows lock/dismount, unique guard consumption and a single existing CDC v1 `RELEASE_STORAGE`. Device backend cutoff-before-ACK, zero deferred writes, USB teardown and durable `SHUTDOWN_ARMED` do not change. Correlated accepted reply plus exact target disappearance alone allows `MAINTENANCE_RELEASE_ONLY`; never `SYNCED` or `SYNC_COMPLETE_RELEASED`.

**Outstanding independent gates:** actual current running handler provenance is `BLOCKED_PROVENANCE`; original historical semantic audit and one-attempt operator permission remain unresolved; real Windows lock/dismount, CDC, Device lifecycle and power failure testing are `NOT_RUN`. Changed-HEAD Engineering, Security and Integration review are required before merge. Any physical action requires its own bounded exact-revision Human Gate. This PR authorizes neither physical operations nor a production release entrypoint.

#119 physical cycles are unchanged (zero). PR #120 remains Draft. #54 EV-018 remains blocked.
