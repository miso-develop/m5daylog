# PC capacity observation (#137, source-only)

## Ownership and admission

This is an advisory read-only component, not a Device discovery, sync,
release, retention, or status-watcher implementation. The #135 coordinator
must supply a user-authorized, already validated #89 EjectTarget paired with
its CDC port and one current, unique session epoch. A matching device.json
alone is not authorization. The #51 durable-sync callback must be true only
after verified local final WAVs and SQLite SYNCED commits, with zero conflicts.

Call probe_capacity **once in the #135 serialized sequence after #51 sync and
before #89 seals the existing UsbSessionIoGate**. The probe borrows an ordinary
MSC lease, invokes the existing WindowsHostReleasePort revalidate_target and
volume_available checks before and after its single read-only
GetDiskFreeSpaceExW query, and reads the frozen Volume GUID root (not a mutable
drive letter). It never calls CDC, creates a Device handle, mutates a file or
requests lock/dismount/eject. The #135 coordinator MUST serialize this call
against gate.seal; this process-local gate is not a cross-process authorization
fence or an atomic lock around GetDiskFreeSpaceExW. #135 owns the separate
cross-process/restart pre-send fence and RTC mutation prerequisites.

## Consumer contract

- READY_FOR_NEXT_16H: usable free >= 1,977,417,728 bytes. This is an
  **estimate**, not a promise that a new 16-hour recording will succeed.
- ACTION_REQUIRED: valid free bytes below threshold.
- UNKNOWN: no numerical estimate. Capacity-only API failure is a warning,
  not an additional release gate.
- release_blocked=True means a failed sync prerequisite, invalid/changed
  physical target/session identity or closed gate. It is NOT a capacity-only
  warning; #135 must fail closed through #89 and must not invoke release.
- The recorded physical total-free count is **not** the available-to-caller
  count. For an advisory usable value take min(caller-free, physical-free).
  Per-user quotas may make physical-free larger than user total capacity.

Render status and free-space information **separately** from sync/release
outcomes. CapacityResult.public_notice() emits no device/host identifiers and
never claims release success. It is the caller's responsibility to check
valid_for(session_epoch=..., session_open=True) before treating the observation
as current. If showing a previously captured successful-session result after
release, mark it **last-verified-session**; it is not a fresh post-release
measurement. Discard at disconnect, re-enumeration, reboot, sleep/resume or the
next recording session. In every view explain that original unACKed WAVs are
not automatically reclaimed.

The 128 MiB reserve is an **advisory** PC cushion, not an allocation guarantee
or firmware threshold change. #36 Device local fail-loud capacity checks remain
authoritative during recording.

## Verification boundary

Run the synthetic tests as part of the PC Python 3.11 suite:

    cd pc
    uv run python -m unittest discover -s tests -p 'test_capacity.py' -v

The adapter is tested using injected fake kernel32 functions; none of these
tests prove native Windows GetDiskFreeSpaceExW behavior on the exact real
MSC Device, pre/post lock timing or real USB release. Actual Windows/Device
observation belongs to a separately authorized #59/#60 exact-revision
Human Gate after #119/#130 provenance and preservation blockers are cleared.
No physical operation is authorized by this document.
