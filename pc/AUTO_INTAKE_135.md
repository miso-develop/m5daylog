# PC Task #135 — source-only intake core (NOT production auto-release)

## Scope and operational state

This Draft candidate implements the **safe subset** of #135: an injectable,
two-scan arrival coalescer, prior user allowlist admission, revalidation of an
already-verified MSC/CDC physical parent and current `USB_SYNC` session proof,
#51's canonical raw WAV/SQLite sync, process-level local intake exclusion,
and #137's pre-seal read-only SD-capacity result. Every post-sync result is
`RELEASE_BLOCKED` with `AUTO_RELEASE_NOT_QUALIFIED` (or a narrower error).

A Windows-only observer is now provided in `windows_auto_intake.py`. It polls
removable Windows drive roots, validates canonical metadata and manifest,
requires the user-allowlisted Device ID, pairs **exactly one** CDC endpoint to
the same MSC physical USB parent via the canonical #89 Win32 adapter, and
reads current `USB_SYNC` via bounded read-only `GET_STATUS`. The observer
uses a fresh validation port per session. All errors or ambiguity fail closed.

An opt-in background/foreground process can run as
`python -m m5daylog.windows_auto_intake` (or `--once` for one bounded
observation), but **must not be run against the existing physical Device as
a Human Gate without separate approval**. A per-user Startup shortcut may
eventually launch the same module after logon; no installer, service or
elevation is added. Status is written as one sanitized local JSON snapshot
(`%LOCALAPPDATA%/M5Daylog/state/auto_intake_status.json`) and optionally
printed in the foreground. A native local Windows warning dialog is displayed
after completed-but-unreleased intake or failed sync, without host/device
identifiers or external dependencies. This is a **blocking/action-required**
notification, never a successful safe-eject notice. Auto-release remains
blocked; therefore this is **not** full daily UX.

The user must create a private, locally stored
`%LOCALAPPDATA%/M5Daylog/monitor.json` containing the explicit opt-in
and trusted Device ID:
```json
{
  "enabled": true,
  "trustedDeviceIds": ["00000000-0000-4000-8000-000000000000"]
}
```
This is an **illustrative synthetic UUID**, never copy an actual user-specific
Device identifier into a public file. `enabled:false` or removing the
configuration disables observation. Do not place user identifiers in CLI
arguments, public logs, Issues or screenshots. A stale/missing configuration
causes no Device read/copy.

## Explicit safety boundaries

- The provided Windows `ArrivalProvider` verifies the same present MSC/CDC
  physical parent, a fresh host observation epoch, current `USB_SYNC` via
  `GET_STATUS`, and the configured user's known Device identity. A supplied
  `Arrival` from a fake/test provider is never itself proof of those facts.
  A host-minted epoch is **not** a durable Device session/release authorization
  or a defense against spoofed USB devices.
- The coordinator verifies that the claimed exact target is in its trusted
  device-ID allowlist, invokes the same #89 target/CDC revalidation port,
  validates canonical metadata/manifest/recording path containment, then
  revalidates before invoking #51 `sync_device` on the selected root.
- A local lock prevents two cooperating monitors from copying concurrently.
  The `sync_device` source remains the only raw-sync implementation; repeat
  intake after a process restart is non-destructive and hash/idempotency
  checked by #51.
- `probe_capacity` runs after #51 normal return with the same
  `VerifiedSession` and open MSC I/O gate, and independently reports
  `READY_FOR_NEXT_16H`, `ACTION_REQUIRED` or `UNKNOWN`. Identity change
  blocks the next operation. A capacity API-only failure is advisory.
- No code path in this module calls `gate.seal()`, `SET_TIME`,
  `RELEASE_STORAGE`, lock/dismount or shell/PnP eject. Neither the local
  intake mutex nor a new epoch is a crash-safe release-attempt fence. The
  shared manual `sync-release` entrypoint still has no such fence.
- No output claims Device `SHUTDOWN_ARMED`, safe USB detachment, guaranteed
  next-day storage, successful overnight charging, or released ownership.
  Output contains only status, safe reason code, sync counts, and advisory
  capacity fields; it excludes device/COM names, local paths, serials, audio.
- No ACK, AI, raw deletion, automatic retention, cloud, firmware or schema
  changes. No privileged service, no persistent Device I/O.

## #135 source-only release prerequisites added (not wired to auto-release)

The canonical `release.py::SyncReleaseCoordinator` now validates the exact
MSC/CDC target **before** any optional RTC CDC mutation, supports an optional
`before_seal_observation(target, gate)` callback after #51 sync and before
the final identity revalidation/seal, and supports
`before_release_send(attempt_id, target)` **inside** the volume lock/dismount
lease and **before** the first possible `RELEASE_STORAGE` send. Failure of
either callback blocks the send; historical #89 manual callers without these
new hooks remain unchanged. This is an interface seam, not release clearance.
A capacity-only API error may be advisory, but a changed/unverified session
must veto release.

The canonical Windows adapter now supports bounded `SET_TIME` with an
exact previously frozen CDC endpoint and strict matching response, including
`eventPending:true`. Invalid timestamp, changed endpoint, negative response,
or indeterminate transport cannot produce RTC success. **No automatic consumer
or existing manual CLI has yet been wired to call this mutation**; merely
having the source does not satisfy the RTC prerequisite.

`release_fence.py::DeviceAttemptFence` implements an intentionally
conservative SQLite `synchronous=FULL`, process-serialized, durable
pre-send reservation **on host media only**. It stores hashes instead of
plain Device IDs and rejects duplicates or any uncertain journal outcome.
Its key is **Device-scoped for all time**, NOT a fabricated USB session key.
A second process, power-cycle or restart cannot claim the same Device after
a potentially sent command. **This cannot provide repeatable daily release**,
and it must not be adopted as a permanent v0.1 workflow or automatically reset
without a separately approved, provable new Device session. It is only a
source-only fail-closed building block tested with fake objects. It is not
currently wired into the manual or automated command. The existing CLI
overlap with paused PR #110 has been permitted by Integration for narrow
future coordination; **no CLI behavior has been changed in this iteration**.

**Blocking product/spec issue:** CDC v1 `GET_INFO` reports deviceId, and
`GET_STATUS` reports USB_SYNC and RTC pending, but neither reports a
durable unique fresh session generation. The host's random
`VerifiedSession.session_epoch`, COM number, Volume GUID, or new
`releaseAttemptId` cannot distinguish a genuinely new Device session
from a re-enumeration after a possibly accepted release. A safe repeatable
daily automatic release therefore requires a Specification decision and
an independently approved cross-domain session authority or equivalent
proof. Until then, do not enable the release sender, clear the journal or
claim PC-A03/V01-RELEASE acceptance.

## Next work required before reviewable end-to-end auto-release

1. Qualify the new Windows-only observer, opt-in/at-login startup, session
   refresh and operator-visible warning on the approved host in a separate
   exact-HEAD Human Gate. A nonmodal notification alternative may require a
   later bounded decision; source-only tests do not establish physical PC-A01/A05.
2. Integrate the smallest shared #89 coordinator seam after durable #51 sync
   and exact target validation, before sealing, for #137 capacity observation.
   Preserve native lock/dismount and dedicated CDC release control.
3. Supply the **real** qualified #21/#50 `SET_TIME` operation before the
   release I/O cutoff; no optional empty callback can serve as success.
4. Coordinate `cli.py` with PR #110's existing reservation and provide a
   durable cross-process, crash/restart-safe pre-send attempt fence that is
   effective for all relevant release senders, including the manual CLI.
   Re-enumeration/port change does not prove a fresh Device session.
5. Add an exact-HEAD Linux and Windows Python 3.12 regression run, simultaneous
   process/kill/restart/RTC/identity/volume fault matrix, independent
   Engineering and Security reviews, then Integration. #119/#130 provenance
   and the real exact-device Human Gates remain **BLOCKED / NOT_RUN**.

## Current test boundary

`pc/tests/test_auto_intake.py` and `pc/tests/test_windows_auto_intake.py`
use only synthetic callbacks and temporary host directories. They check
denial before copying, conflicting/outdated identity, a competing local
lock, idempotent repeated sync model, #137 advisory/error, USB_SYNC/MSC/CDC
parent matching, opt-in, poll coalescing and status redaction. Existing
#51/#89/#137 test suites remain normative. This candidate has **not** been qualified with a real Windows
arrival, physical USB, CDC or Device.

The correct completion state of #135 after this limited patch remains
**IN PROGRESS**, not V01-AUTO/V01-RELEASE PASS.

## #143 R3 source-only continuation: cycle witness + dual local continuity

Human-approved #143 R3 Option A replaces the earlier *device-scoped-for-life*
placeholder as the **future** normal-path release design. The older
`DeviceAttemptFence` class remains unused for compatibility with existing
source-only tests; it is **not** a daily-session authority.

- `windows_release.py::read_status` now recognizes optional
  `GET_STATUS.result.releaseCycleId` only when a strictly lowercase canonical
  UUID. Missing or malformed tokens permit independent read-only intake but
  provide **zero release authorization**. This parse step does not establish
  Device current-WAKE provenance, final nonzero PCM, durable manifest binding,
  or valid session ownership: all remain mandatory before use.
- `release_cycle_fence.py::CycleAttemptFence` is an **unwired** host-owned
  release-cycle reservation prototype. It has a SQLite `synchronous=FULL`,
  sequence + SHA-256 chain of hashed consumed tuples, a separately stored
  per-user Windows HKCU continuity anchor with `RegFlushKey`, explicit
  nonautomatic supervised-initialization entrypoint, and a cooperating
  per-user OS file lock. The canonical Windows factory selects a single fixed
  LOCALAPPDATA ledger/lock for future CLI and watcher integration. If either
  witness is absent, corrupt, rewound alone, or mismatched after a partial
  commit, normal release is blocked without auto-repair. A consumed tuple is
  not cleared after no-send exceptions, lost response or a new host epoch.
- Synthetic unit tests cover two distinct eligible IDs, duplicate denial,
  independent process competition, each one-sided rollback, interrupted
  anchor update, missing anchor, corrupted chain, and optional CDC status
  decoding. They **do not** establish Windows physical power-loss durability.

**Not yet implemented / STOP:** Neither this new ledger nor the existing
`release.py` pre-send hook is wired into `sync-release` or the monitor.
The current manual command has no R3 cycle gate; the Draft PR must not merge
as a release-capable change while this bypass exists. The Device-side
releaseCycleId producer is #144, and no accepted Device proof is available
yet. Canonical manifest/witness binding, RTC-before-seal failure journal,
CLI+watcher shared full-lifetime coordinator lock, pre-send consumed
reservation, supervised enrollment/recovery UX, positive two-day integration,
real Windows HKCU/SQLite flush/crash qualification and exact-Device Human
Gates remain outstanding. **Auto-release stays OFF, all watcher post-sync
results remain RELEASE_BLOCKED, and no CDC release or real Device operation
was exercised.**

The approved Option A security boundary is deliberately bounded to one
cooperative enrolled Windows user and physically controlled known Device.
Matched whole-host rollback, malicious same-user bypass, and hostile USB
impersonation are **excluded limitations**, not security tests passed or
cryptographic guarantees. No user/device identifiers or private paths may be
logged or embedded in public evidence.


## #143 R5-FU bounded first-use preflight (source-only, no release sender)

\`first_use_admission.py\` models only a **negative admission check** for the
independently reviewed and Integration-adopted R5-FU *single #119 Device* initial
enrollment design. It accepts synthetic evidence bundles representing a
separately authorized supervised staging intent, exact prior #105 maintenance
attempt/accepted host and Device release witnesses, an independently verified
never-enrolled host, and a distinct subsequent qualified first-use recording
boot. Its result is a planning tuple, **not** enrollment, permission, durable
intent consumption, or authorization to transmit a CDC command.

- An R5-capable host must distinguish \`SUPERVISED_FIRST_USE\` from
  \`NORMAL_WAKE\`. Unknown/missing origin and legacy cycleId-only status cannot
  authorize an unattended release. The enum names here are **provisional
  source-only fixtures**, pending Integration-owned exact CDC wire freeze.
- A pre-existing enrolled, mismatched, indeterminate or partially restored host
  cannot enroll again, including when both host witnesses appear absent. An
  independently retained supervised never-enrolled receipt is required.
- A consumed/uncertain intent, absent human permit, unqualified running image,
  unverified protected-media state, wrong maintenance attempt/session,
  unaccepted release, missing durable SHUTDOWN_ARMED, stale/uncorrelated
  subsequent session, non-final PCM, invalid RTC, or incomplete sync fails
  closed. Host status booleans/strings alone are **not provenance**: the
  integrator must establish all positive fields from independent evidence.
- The R5 preflight deliberately has **no filesystem, Registry, USB, CDC, NVS or
  Device-media IO**, does not call \`CycleAttemptFence.enroll\`, and is **not
  wired** to the watcher or existing manual CLI. Source-only tests
  (\`test_first_use_admission_143.py\`) use synthetic identifiers only.

**Remaining mandatory implementation** (not silently complete): persist and
single-use-consume exact host intent/approved attempt; durably initialize/re-read
the SQLite and independent Windows continuity witnesses and consume the first
cycle tuple **under the same global lock before any possible CDC send**;
distinguish already-enrolled *on transaction entry* from the state created by
the supervised first-enrollment transaction; fail permanently closed on
split/unknown commits, restart, one-sided rollback and uncertain CDC response.
Implement and independently review the shared manual CLI+watcher guard,
provisional CDC origin compatibility, real Windows fault/durability tests and
Device-side qualified first-use source. Do not use this prototype as evidence
for PC-A03, physical #119 operations, RELEASE_STORAGE, merge or unattended
release. #119/#130 firmware provenance and separately permitted physical Human
Gates remain BLOCKED / NOT_RUN; the canonical \`#134\` daily objective is unchanged.


## R5-FU initial host transaction — isolated source-only model

The new `first_use_transaction.py` is NOT installed into the watcher,
`sync-release`, release sender, or real supervised enrollment process.
It consumes an **externally issued** one-use #119-bound host-intent file
and invokes `CycleAttemptFence.initialize_first_cycle_locked` under
the existing R3 cooperating OS-wide lock. The latter initializes SQLite
with the **first consumed cycle tuple in the same database commit**, then
flushes/re-reads the independent anchor. This prevents a locally created
first enrollment from being misclassified as a pre-existing one and avoids
an unfenced empty-ledger window. No new R3 ledger schema, CDC contract, or
Device/media mutation is introduced. The legacy `enroll(supervised=True)`
remains a separate unwired R3 placeholder; it must not serve as the R5
initial-enrollment transaction.

The source-only ordering is:

1. Lock, check actual SQLite/anchor absence **and independently verified
   never-enrolled history**, original Device/Human/#119 evidence, and strict
   provisional first-use origin and manifest/RTC/target proof. Empty stores
   alone are **never** a permission or independent continuity proof.
2. Read the externally supplied persisted intent, require exact original
   #119 Device/physical-parent/session/maintenance-attempt binding and an
   unconsumed one-shot state. The production-grade supervised *issuer*,
   current running-image/custody attestation and exact operator permit remain
   unimplemented. Host-local SHA-256 fields only avoid exposing raw IDs in
   the prototype; they are not signatures, encrypted receipts, or guarantees
   against malicious same-user writers.
3. Consume the intent via an exclusively created, fsynced pending-replacement
   marker; any stale partial marker, missing/invalid receipt, uncertain write
   or post-consumption crash blocks without self-repair or automatic retry.
   A deliberately separate intent file is not the independent HKCU continuity
   witness. A source-only file fsync/rename is **not** qualified Windows
   power-failure durability.
4. Initialize the SQLite chain with the first consumed
   `(deviceId, releaseCycleId, releaseAttemptId)` tuple, update/flush HKCU
   anchor, reread both and verify exactly the same tuple before a return.
   A SQLite/anchor split, process crash, previous enrollment, rollback of
   either witness, or competing process blocks; no recovery or second
   first-use authorization is inferred. The synthetic model returns **no
   authorization token** and does not transmit SET_TIME/RELEASE_STORAGE.

`test_first_use_transaction_143.py` exercises the positive *synthetic*
first-cycle state, a distinct subsequent R3 tuple, missing or consumed
receipt, stale #119 attempt, invalid origin/PCM/RTC predicates, already
enrolled/corrupt host states, interruption after intent consumption,
SQLite/anchor split commit, crash after both writes, one-sided SQLite
rollback, and two independently spawned contending processes. The fixtures
stage a sample intent directly and use a fake file anchor; **they do not
provide a real #119 supervised receipt, real HKCU/RegFlushKey power-loss
qualification, actual Windows at-login UX, current Device firmware or a
Human Gate**. Additional fault tests at actual Windows persistence/USB/CDC
boundaries remain mandatory for production release acceptance.

**STOP lines unchanged:** R5 `releaseCycleOrigin` is provisional pending
the separate #143 CDC compatibility review. No caller may treat the source
model or its caller-provided `*_verified` booleans as independent proof.
Real Device #144, #119/#130 preservation/provenance, exact manifest
terminal-segment/RTC validation, CLI+watch single full-call coordinator,
unattended release sender, Windows crash durability, exact-head independent
Engineering/Security and separately authorized physical Human Gates are
outstanding. PR #142 remains Draft; watcher `RELEASE_BLOCKED` is unchanged;
no merge, CDC release, RTC/SD/NVS action or claimed PC-A03 PASS is permitted.

## R5 CDC v1 origin-pair compatibility (read-only source milestone)

The frozen source-only CDC contract in #143 requires releaseCycleId and
releaseCycleOrigin to be present together in one matched GET_STATUS result.
The normalized Windows decoder preserves the pair only when the cycle is
strictly lowercase canonical UUID and the origin is exactly NORMAL_WAKE or
SUPERVISED_FIRST_USE. Missing, malformed, unknown, legacy cycle-only and
one-sided values produce no release witness; independent read-only WAV
intake remains possible. Duplicate JSON keys fail the entire status parse
instead of silently taking the last value. A stale envelope ID or CDC endpoint
still blocks the status observation.

The read-only manifest candidate now carries the source-owned CycleOrigin
enum, so a future caller MUST route NORMAL_WAKE through a previously-enrolled
ordinary flow and SUPERVISED_FIRST_USE through the supervised never-enrolled
flow; it must reject missing/unknown origins without fallback. Source-only
candidate generation is never a permit to send or enroll. A successful
in-session SET_TIME may leave rtcCorrectionPending=true with the frozen pair;
the bit in either state does NOT prove RTC validity, Device boot lineage,
terminal current-run PCM, manifest hash/sync, or physically qualified custody.

Fake-port tests cover paired and legacy/one-sided/null/noncanonical values,
invalid origin casing and whitespace, duplicate envelope/result keys, stale
response IDs, changed endpoints, stale epochs, ordinary versus supervised
candidate tagging and positive pending-audit observation. No actual CDC
SET_TIME/RELEASE_STORAGE command was added. An old origin-unaware executable
can still ignore the new field: this change does NOT disable the legacy
manual sync-release sender or qualify R5 firmware deployment. Full shared
CLI/watch admission, externally issued one-time intent, current-running
Device R5 producer, Windows durability and exact physical Human Gates remain
blocked. PR stays Draft; watcher RELEASE_BLOCKED; no merged release authority.
