# #148 DEV_FRESH_INSTALL — offline preview only

This is a **pure in-memory, source-only preflight**, NOT an installer.
It has no enabled production CLI entrypoint and does not read input files,
Windows HKCU, release ledger, USB/CDC, Device NVS, or live SD. For R it
reads only the repository-bundled canonical JSON schemas via the existing
contract validator; it does not access a mounted Device or user data. It does not
flash, format, erase, release storage, copy audio, or mutate the supplied
snapshot. Physical authorization and exact-device proof are separate gates.

## Input contract

Call preview_install(offline_fixture) from m5daylog.dev_install_preview_148.
Provide a dict holding these independent pieces of **offline** evidence:

- branch: C (independently blank/new media), R (settled retained media), or
  A (fully archived and separately approved for retirement).
- target: targetBinding, hostBinding, distinct canonical UUID generation
  and attemptId. C/A require an independently recorded **random v4**
  install generation; their new candidate Device UUID is derived from that
  generation, branch and target/host (UUIDv5). Reusing a generation therefore
  reproduces the **same** candidate and is not authorization to install.
  This remains an offline plan, not a live Device identifier witness.
- host: matching hostBinding; lineage NEVER_ENROLLED; a nonempty
  independentLineageEvidence; releaseSQLite and releaseHKCU both ABSENT;
  sharedHostCoordinatorBaseline INDEPENDENTLY_OBSERVED,
  historyComplete true, earlierReleaseAttempt false.
  The module does not actually read host stores. In production, host H0
  must be independently proven under the shared #135 coordinator lock.
- approval: purpose DEV_FRESH_INSTALL_PREVIEW, matching branch, target,
  host, generation and attempt; nonempty externalEvidenceRef;
  intentStatus UNISSUED_FOR_REVIEW and priorConsumedOrReplayed false.
  approval.imagePlanSha256 must match the SHA-256 of canonical JSON encoding
  (UTF-8, sorted keys, compact separators) of the validated exact image plan,
  including source/ELF/APP/bootloader/partition hashes, flash/app/NVS layout
  and intended writeRegions. approval.deviceId must match the branch-selected
  retained or derived Device UUID. Changing an otherwise valid image digest
  or geometry without changing this comparison pin yields BLOCKED.
  The approval object is caller-supplied and unauthenticated: traceability
  for proposed external review, NEVER a verified release permit.
- custody: writerExcluded true, inventoryComplete true, and
  externalCustodyEvidence. These are untrusted offline assertions;
  a real single-writer boundary needs independent Human Gate evidence.
- image: lowercase 64-hex hashes sourceSha256, elfSha256, appSha256,
  bootloaderSha256, partitionTableSha256; 4-KiB-aligned nvsOffset/nvsSize;
  flashSizeBytes, appOffset and appSize must also be supplied;
  writeRegions must specify exactly the matching NVS partition.
  This checks intended geometry and NVS/APP nonoverlap, NOT actual
  installed image or boot.
- media: a mapping of files from M5DAYLOG-relative filename to full
  immutable bytes, and listedPaths comprising their complete names.
  All listed files must have bytes. Missing/unlisted files block; R allows
  fully inventoried ancillary files outside recordings/ and hashes them.

For R: add retainedDeviceId equal to the identity in device.json and
manifest.json. The preview checks actual supplied WAV bytes, size, full
SHA-256, path, recordingId uniqueness, and the settled manifest. Pending
.bak/.tmp/.part transaction files, orphan WAV, unindexed recording
artifacts and inconsistent schema block. No historical WAV or manifest is rewritten; future
append-only behavior belongs to Device #144.

For C: media files/list must be empty, independentBlankEvidence must be
supplied, and freshIdentity must include independentGenerationEvidence,
priorIdentityInventoryComplete true and knownPriorDeviceIds (a complete
offline witness list, possibly empty). The code derives one UUIDv5 candidate
from the externally issued random v4 install generation. Optional
newDeviceId, if supplied, must equal this derived ID; it cannot silently
substitute a preexisting UUID. A derived ID present in knownPriorDeviceIds
or contradictory supplied NVS/host evidence blocks. This is only a
**self-consistency check against claimed independent history**; it cannot
prove the physical host/device never used that UUID. Missing manifest alone
cannot prove an empty SD.

For A: independently fresh-generation identity evidence as for C, plus
archive containing snapshot (all original files and listed paths),
noClobberWitness true, retirementApproved true, retirementEvidence,
newBlankMediaEvidence, destinationEvidenceRef, a full verifiedInventory
of per-file SHA-256 and size, and newMediaFiles empty. Its new candidate
Device UUID is derived from the independently asserted generation and is
**different from the R retained ID**. The preview checks complete byte
hashes across source and archive, without moving/deleting any files or
validating actual human signatures.

## Output is never a permit

BLOCKED results contain only a bounded reason string and no candidate.
CANDIDATE_PREPARED returns a deterministic non-executable model with
authority NONE_NOT_A_PERMIT and physicalGateStatus
NOT_RUN_NOT_AUTHORIZED. The proposed desired NVS state is device_id,
release_boot FENCE_UNQUALIFIED, shutdown_armed NORMAL, RTC UNQUALIFIED;
actual NVS key encodings and partition layout must be independently
reconciled against Device #144 before any physical plan.

The candidateDigestSha256 and imagePlanSha256 are unkeyed comparison
fingerprints, not proof of originality, a signature, or a consumed
authorization. An attacker can change the caller-supplied image and its
untrusted approval pin together: only separate independent permission
validation can establish an authoritative image/target binding. The
first real supervised-release attempt consumption and later distinct
first enrolled cycle consumption remain owned by #135. A prior
single-store receipt rollback cannot reauthorize a used attempt.
Matched whole-host rollback has only the previously approved #143 R3
Option A threat boundary; no new exclusions are authorized.

## Scope / testing / known negative coverage

Run with Python 3.12 in the PC project:

    python -m unittest discover -s tests -p "test_dev_install_preview_148.py" -v

The tests use synthetic bytes only and check every denial path for nested
fixture/byte immutability; cases cover valid-hex image substitutions,
partial permits and partition plans, changed-generation UUID candidates,
known-prior-ID replay and conflicting R restoration. Private audio, host
identifiers and Device identity should not be published in public CI logs
or Issues.

R may retain non-recording ancillary files (including historical ACK or
event files) only as fully inventoried opaque bytes with captured hashes;
it does not interpret those as processed authority or edit them. Future
schema-specific authorization for ancillary content needs separate review. Python-level snapshot identity or preflight H0 does not prove
live Device/host physical custody, actual image readback, or committed
Windows storage. Human Gates and >=16h overnight tests remain NOT_RUN.

No existing #135/PR #142, #144/PR #146, CDC contract, Device code or
release sender is changed. Source-only independent Engineering/Security,
Integration, and separately human-approved physical gates are mandatory.
