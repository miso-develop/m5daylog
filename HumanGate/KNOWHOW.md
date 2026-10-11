# Device Human Gate — source-backed know-how

Non-normative. See Issue #87, #50, #51, Decision #105 and .agent/HUMAN_GATE/*.

## PowerShell execution and output contract

- One outer & { ... } per executable packet; do not rely on variables left by a previous packet.
- Initialize phase/result, use try/catch/finally, and avoid early return before the footer.
- While a long operation executes, print bounded PROGRESS messages so operators can distinguish slow work from a hang; do not treat progress as completion.
- At the END print a blank line, exact ==== $phase ==== heading, all decision-relevant fields, one SUMMARY phase=... diagnosis=... disposition=..., then a blank line.
- Preserve exact native process exit or UNKNOWN/NOT_RUN; a cleanup failure after child start is never proof the whole tree exited.

## First-use setup and target identity

- The actual supported toolchain is ESP-IDF v5.5.5 managed Python + canonical idf.py. A Windows python alias or idf.py wrapper may resolve incorrectly.
- Git HEAD + a clean tracked worktree do not qualify ignored sdkconfig, components and output. Bind effective configuration, generated flash plan, all image digests/offsets; do not permit an implicit rebuild after qualification.
- Qualify current USB MSC/CDC using stable Device and storage evidence, not solely a drive letter, a new PnP identity or a VID/PID.
- A read-only NVS/flash operation may leave ROM mode or reset/re-enumerate: always qualify a NEW Device runtime baseline afterward.

## NVS: preserve evidence before fixture repair

Do NOT clear NVS unconditionally at start. A blanket erase can remove device_id, SHUTDOWN_ARMED, HOST_UNRESOLVED, RTC pending and evidence required by the test. #87 had a HOST_UNRESOLVED baseline: the operator performed a *specifically authorized* Device-ID-only fixture restoration, verified NVS read-back and then requalified NORMAL. This was setup/repair and counted ZERO cycles. Other AC intentionally require SHUTDOWN_ARMED or RTC pending persistence.

Safe sequence:
1. Confirm exact HEAD, approved matrix and NVS partition offset/size from its qualified partition table.
2. Read/preserve NVS before changing it. Inspect lifecycle (NORMAL/SHUTDOWN_ARMED/HOST_UNRESOLVED/AMBIGUOUS), the fixture identity and required baseline.
3. If and only if approved setup requires reset, obtain explicit Human authorization for an exact image and target, retain prior evidence, write only the approved fixture and verify exact read-back hash.
4. Re-enter normal boot using the verified hardware procedure, establish new MSC/CDC baseline, record cycles_counted=0. Never auto-clear NVS as error recovery or to make a product FAIL pass.

## D-031 release and host quiescence

- Verify source raw sync/SQLite completion, seal new I/O, close handles, freeze exact volume identity, FSCTL_LOCK_VOLUME then FSCTL_DISMOUNT_VOLUME while lock held.
- Then (and only then) send ONE CDC RELEASE_STORAGE request with fresh request id and releaseAttemptId, match accepted response, observe complete bounded teardown.
- If lock fails before request_sent, close host consumers and retry only the release phase when earlier snapshots remain valid. If request might have been sent, NEVER resend on timeout, serial close/dispose error or loss of response.
- Accepted response and complete teardown remain valid when only serial-close cleanup failed. Shell safe-remove, cable detach and suspend are not alternative release authority; negative authority testing requires fresh baseline without sending RELEASE_STORAGE.

## #87 WAKE / recording cycle

- After release: USB physical removal, manual WAKE with USB absent, bounded recording dwell, reconnect without RESET/BOOT, requalify MSC+CDC.
- Require one finalized new WAV, nonempty PCM, valid header and duration/size/SHA-256 matching manifest, no corresponding .part. A 44-byte/0ms file is not successful fresh recording.
- Increasing a recording-evidence dwell (180 s worked in the #87 harness) is an observation accommodation, NOT a universal minimum PCM acceptance threshold. If WAKE was not proven, counted=false and BLOCKED/NOT_RUN depending on phase; do not infer it from output.

## #50 read-only CDC and RTC recovery

- The successful four-command evidence consisted of current-head GET_STATUS before, PING, GET_INFO, GET_STATUS after, plus previously accepted SET_TIME evidence. The addendum did NOT send SET_TIME again; it also confirmed metadata was unchanged.
- AC-15's accepted SET_TIME was protected by a one-shot guard, NVS RTC pending was checked, a single canonical release/teardown was performed, ROM NVS read verified SHUTDOWN_ARMED and pending, manual WAKE recovered exactly one event before fresh recording. 3/3 separately qualified cycles passed at the recorded HEAD; do not reuse them at a new HEAD.
- Never invent an events.jsonl USB_SYNC event that is not persisted by the product. Do not impose a 180 s PCM minimum absent from approved AC. Historical zero-PCM WAVs remain separate diagnostics.

## #51 interrupted incremental sync: PC-owned boundary

- Preserve local .wav.partial on interruption; after re-enumeration revalidate exact Device/manifest and use hash-aware resume, finalize local SQLite state, then verify idempotency (no duplicate rows, conflicts, partials or additional copy on second pass).
- A successful sync result is NOT release authority. Reuse existing PC-domain implementation/receipts rather than inventing a Device script which mutates PC sync state. The pre-interruption hash/identity evidence must be retained.
- Where possible combine nonmutating environment checks and re-enumeration validation into one bounded command; keep destructive/one-shot phases separate.

## Evidence

Separate automated, command-wrapper, human physical observation, and acceptance verdict. Keep COM, MAC, PnP, NVS raw dumps, full local paths and audio private. Public serialized text must pass scripts/security_scan.py --public-text-stdin/--public-text-file before controlled GitHub publication. Source-backed know-how is reusable, earlier exact-revision Human Gate verdicts are not.
