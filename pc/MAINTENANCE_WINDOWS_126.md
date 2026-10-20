# #126 Windows maintenance qualification — Phase A source-only model

Scope: [Task #126](https://github.com/miso-develop/m5daylog/issues/126). Authority: [D-031 maintenance-only amendment](https://github.com/miso-develop/m5daylog/issues/105#issuecomment-6267701110) and [S-004 amendment](https://github.com/miso-develop/m5daylog/issues/37#issuecomment-6267717379). Upstream source candidate: PR #125, not yet merged.

## Current gate: software trace model, not native attestation

`maintenance_windows_qualification.py` is a pure, zero-I/O, synthetic state/trace verifier. Its positive result `SOURCE_TEST_ONLY` **never** sets `release_authorized`; `native_qualification` is always `NOT_RUN`. Even perfect synthetic traces and mock directory-flush statements are not evidence of true lock ownership, live on-media snapshots, Windows crash durability, or a running Device's D-031 handler provenance.

The model is deliberately independent of PR #125's `MaintenanceReleaseCoordinator`. Nothing imports it from the public CLI; no new production release entrypoint, volume open, USB/CDC operation or guard file write is introduced. Existing `WindowsHostReleasePort` and ordinary sync/release behavior remain untouched.

## Necessary native proof: still absent

PR #125 expects `prove_locked_snapshot(target, fresh_preflight)` **after** `FSCTL_LOCK_VOLUME` and `FSCTL_DISMOUNT_VOLUME`, while the original volume-lock handle remains held. Current merged `WindowsHostReleasePort` exposes the lock/dismount context but does not expose a trustworthy post-lock filesystem snapshot, disk mutation exclusion proof, or exclusive same-handle attestation.

The sequence alone **does not prove** that an earlier Python directory walk remains current after lock. A later read through a reopened DOS path may be unavailable after dismount and cannot by itself prove it is bound to the original lock handle. An OS-only PnP identity recheck is likewise not equivalent to media-content freshness. A proposal using raw volume handle I/O after lock/dismount must prove relevant Win32 ABI details and invariant-preserving access under an independent native Windows acceptance plan; this task does not assume that works.

The eventual trusted witness must account for: exact volume GUID/disk instance/physical USB and CDC correlation, no ordinary MSC/CDC leases, no deferred writes, no Device or external writer, namespace or media switch between the last inventory and retained lock, lock acquired through authoritative Win32 API, and a fresh verification bound to that same lock/session. If any proof is unavailable, report `BLOCKED` without consuming a release permit or issuing CDC bytes. Fake `post_lock_witness` trace events in these tests do not meet this requirement.

## Guard: modeled at-most-once is not Windows power-loss durability

PR #125's `O_EXCL` creation and file `fsync` intentionally fail closed on local errors, but they do not alone establish a globally canonical host volume/namespace or persistence of the directory entry through Windows crash/power loss. A guard lost on power failure could re-admit an already possibly transmitted attempt; this must **not** be treated as safe because two process-local simulated guards agree.

`SyntheticGuard` exercises reservation-before-possible-write, fail-closed uncertain writes, duplicate consumption and simulated restart without writing any real journal. Its in-memory `simulate_restart` is **not** evidence of actual process restart, OS cache flush or NTFS/FAT32 directory-entry durability. Native qualification must separately prove host-local trust/volume identity, canonical alias/junction resistance, crash recovery and one-way terminal attempt consumption. Until then, real release remains unavailable.

## Phase A evidence and pending work

| Evidence class | Current scope | Disposition |
| --- | --- | --- |
| Python source-model ordering and negatives | Synthetic only | test result must be recorded against exact HEAD |
| Windows-native locked-snapshot API/ABI | No Device or native lock performed | NOT_RUN |
| Actual crash/power-loss directory durability | No crash or power-loss experiment | NOT_RUN |
| Real MSC/CDC/Device current firmware | No device action | NOT_RUN / BLOCKED_PROVENANCE |
| Human Gate approved release attempt | None | NOT_RUN, 0 cycles |

Phase B requires a fresh Integration ownership decision before any edit to PR #125's files, shared release APIs, or any physical/native release-adjacent operation. Independent Engineering and Security Review of exact HEAD are required before merger. Approval of Phase A does **not** authorize a physical attempt or imply that a technically unavailable native proof may be replaced by a weaker signal.
