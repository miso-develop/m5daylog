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
