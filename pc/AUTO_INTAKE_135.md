# PC Task #135 — source-only intake core (NOT production auto-release)

## Scope and operational state

This Draft candidate implements the **safe subset** of #135: an injectable,
two-scan arrival coalescer, prior user allowlist admission, revalidation of an
already-verified MSC/CDC physical parent and current `USB_SYNC` session proof,
#51's canonical raw WAV/SQLite sync, process-level local intake exclusion,
and #137's pre-seal read-only SD-capacity result. Every post-sync result is
`RELEASE_BLOCKED` with `AUTO_RELEASE_NOT_QUALIFIED` (or a narrower error).

**No background autostart, Windows discovery/status provider, installer,
desktop notification integration, or live Device operation is wired yet.**
This module is a source-only component, not a working daily-attach feature.
It must not be launched against a physical Device as a Human Gate. Synthetic
callers may invoke `PollingMonitor.poll_once()` with a fake `ArrivalProvider`.

## Explicit safety boundaries

- The `ArrivalProvider` must establish the same, present USB physical parent
  for MSC and CDC, fresh epoch, current Device `USB_SYNC` state, and the
  configured user's known Device identity; a supplied `Arrival` object is
  not itself proof of any of those. Production providers **must not** set
  `usb_sync_confirmed=True` without actual proof. No production provider is
  shipped in this candidate.
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

## Next work required before reviewable end-to-end auto-release

1. Supply a real Windows-only read-only arrival/USB-session observer and
   explicit per-user opt-in/trusted-ID configuration, foreground status and
   login startup/disable UX. A first-seen Device must not silently authorize.
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

`pc/tests/test_auto_intake.py` uses only synthetic callbacks and temporary
host directories. It checks denial before copying, conflicting/outdated identity,
a competing local lock, idempotent repeated sync model, #137 advisory/error,
poll coalescing and status redaction. Existing #51/#89/#137 test suites remain
normative. This candidate has **not** been qualified with a real Windows
arrival, physical USB, CDC or Device.

The correct completion state of #135 after this limited patch remains
**IN PROGRESS**, not V01-AUTO/V01-RELEASE PASS.
