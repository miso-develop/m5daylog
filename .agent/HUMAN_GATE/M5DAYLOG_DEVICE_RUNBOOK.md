# M5Daylog Device Human Gate Operator Runbook

status: operational supplement
scope: Windows host + M5Capsule / ESP32-S3 Device Human Gate
authority: approved Spec / Decision / Acceptance Criteria remain normative

## Purpose

This runbook records the repeatable operator path that proved effective during the Strategy 2 USB lifecycle Human Gate. It exists to reduce setup churn, prevent invalid cycle counting, and make recovery from host/tooling failures deterministic.

This file does not replace Acceptance Criteria. When it conflicts with the current Issue / Spec / Decision or the repository Human Gate rules, the current approved requirement wins.

## 1. Golden rules

1. Test one exact revision. If HEAD changes, stop and start a new evidence session.
2. Use the ESP-IDF managed Python and the canonical esp-idf tools/idf.py script explicitly. Do not rely on a bare idf.py command resolving correctly.
3. Establish a qualified Device baseline before counting any cycle.
4. Every PowerShell packet uses one outer & { ... } block and reaches one common SUMMARY footer.
5. RELEASE_STORAGE is sent at most once per release attempt. After request transmission or accepted response, never retry it merely because cleanup or later observation fails.
6. Separate harness/tooling failures from product failures.
7. Low-level flash/NVS reads may change runtime state even when persistent data is read-only. Re-establish the application baseline afterward.
8. Keep physical instructions explicit and one action at a time. For this project, operator-facing ACTION= and WAIT= text should be Japanese.
9. Do not publish machine-local paths, usernames, serial numbers, or raw captured audio in durable public evidence.

## 2. Known-good Windows / ESP-IDF preparation

Use placeholders in durable documentation. Resolve concrete paths only in the local shell.

Recommended PowerShell preflight:

~~~powershell
& {
    $expectedHead = "<revision>"
    $repoRoot = Resolve-Path "<repo-root>"
    $idfRoot = "<esp-idf-root>"
    $idfTools = "<idf-tools-root>"
    $python = "<idf-managed-python>"
    $idfPy = Join-Path $idfRoot "tools\idf.py"

    Set-Location $repoRoot

    $head = ([string](git rev-parse HEAD)).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -ne $expectedHead) {
        throw "HEAD mismatch"
    }

    $tracked = @(git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0 -or $tracked.Count -ne 0) {
        throw "Tracked worktree is not clean"
    }

    foreach ($required in @($idfRoot, $idfTools, $python, $idfPy)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Required ESP-IDF path is unavailable"
        }
    }

    $env:IDF_PATH = $idfRoot
    $env:IDF_TOOLS_PATH = $idfTools
    $env:PATH = "$(Split-Path -Parent $python);$env:PATH"

    . (Join-Path $idfRoot "export.ps1")

    & $python $idfPy --version
    if ($LASTEXITCODE -ne 0) {
        throw "Canonical ESP-IDF version check failed"
    }

    Write-Host ""
    Write-Host "==== HG-PREFLIGHT ===="
    Write-Host ("head={0}" -f $head)
    Write-Host ("tracked_dirty={0}" -f ($tracked.Count -ne 0))
    Write-Host ("idf_python={0}" -f $python)
    Write-Host ("idf_script={0}" -f $idfPy)
    Write-Host "SUMMARY phase=HG-PREFLIGHT diagnosis=OK disposition=PASS"
    Write-Host ""
}
~~~

Important lessons:

- A Windows application alias named python can resolve ahead of the ESP-IDF managed interpreter.
- An idf.py.exe helper/shim can report its own wrapper version rather than the actual ESP-IDF version.
- For acceptance-relevant commands, invoke the managed Python plus the canonical tools/idf.py path directly.
- Source export.ps1 only after IDF_PATH, IDF_TOOLS_PATH, and the managed Python directory are set.
- A host reboot normally requires environment reactivation and a fresh Device baseline; it does not by itself require reflashing unchanged exact-head firmware.

## 3. Exact build identity

For a clean exact-head firmware qualification:

~~~powershell
& {
    $repoRoot = Resolve-Path "<repo-root>"
    $idfRoot = "<esp-idf-root>"
    $python = "<idf-managed-python>"
    $idfPy = Join-Path $idfRoot "tools\idf.py"
    $firmwareRoot = Join-Path $repoRoot "firmware"

    Push-Location $firmwareRoot
    try {
        & $python $idfPy fullclean
        if ($LASTEXITCODE -ne 0) { throw "fullclean failed" }

        & $python $idfPy build
        if ($LASTEXITCODE -ne 0) { throw "build failed" }

        $app = Join-Path $firmwareRoot "build\m5daylog.bin"
        if (-not (Test-Path -LiteralPath $app)) { throw "app binary missing" }

        $appHash = (Get-FileHash -LiteralPath $app -Algorithm SHA256).Hash.ToLowerInvariant()

        Write-Host ""
        Write-Host "==== HG-BUILD ===="
        Write-Host ("app_sha256={0}" -f $appHash)
        Write-Host "SUMMARY phase=HG-BUILD diagnosis=OK disposition=PASS"
        Write-Host ""
    }
    finally {
        Pop-Location
    }
}
~~~

Use the build-generated flash plan. Do not guess partition offsets. Do not erase NVS unless the approved setup explicitly requires a fixture reset. Fixture repair/reset is setup and counts as zero acceptance cycles.

## 4. Flash and runtime baseline

For normal exact-head flashing, use the canonical managed Python + idf.py invocation and the intended serial port:

~~~powershell
& $python $idfPy -p "<serial-port>" flash
~~~

Do not use erase-flash as a convenience step in a Human Gate.

After flash, fixture restore, ROM-mode diagnostic, NVS read, host reboot, or any operation that can reset/re-enumerate the Device, establish a fresh runtime baseline before counting a cycle.

A qualified USB publication baseline should prove all applicable items:

- exactly one intended USB mass-storage target;
- FAT32 filesystem;
- expected M5DAYLOG directory structure and manifest are readable;
- exactly one intended CDC interface;
- manifest state is internally consistent;
- no stale .part file relevant to the cycle;
- current exact revision/build identity is already established.

Do not identify the target only by a reused drive letter or by the appearance of a new PnP identity.

## 5. PowerShell packet pattern

Every acceptance phase should follow this structure:

~~~powershell
& {
    $phase = "<phase>"
    $expectedHead = "<revision>"
    $continue = $true
    $diagnosis = "UNKNOWN"
    $disposition = "BLOCKED"

    try {
        # prerequisite observation
        # one intended mutation / physical action
        # bounded observation
        # acceptance-specific validation
    }
    catch {
        # classify tooling/harness vs product result
    }
    finally {
        # guarded cleanup only
    }

    Write-Host ""
    Write-Host "==== $phase ===="
    Write-Host ("expected_head={0}" -f $expectedHead)
    Write-Host ("diagnosis={0}" -f $diagnosis)
    Write-Host ("disposition={0}" -f $disposition)
    Write-Host (
        "SUMMARY phase={0} diagnosis={1} disposition={2}" -f
        $phase, $diagnosis, $disposition
    )
    Write-Host ""
}
~~~

Avoid early return paths that skip the common summary.

## 6. D-031 canonical release sequence

The successful Windows-side order is:

1. Revalidate the exact intended Device/volume.
2. Stop new host-side Device I/O and close application-owned volume handles.
3. Open the exact volume for control.
4. FSCTL_LOCK_VOLUME succeeds.
5. While the lock is held, FSCTL_DISMOUNT_VOLUME succeeds.
6. Only then open/use the dedicated CDC release lease.
7. Send exactly one RELEASE_STORAGE request with a fresh request id and releaseAttemptId.
8. Require the matching accepted=true response.
9. Do not send another RELEASE_STORAGE in that session.
10. Observe bounded complete teardown.
11. Continue to physical removal / manual WAKE only after the required release/teardown proof is present.

Windows control values used by the successful harness:

- desired access: 0xC0000000
- share mode: 3
- FSCTL_LOCK_VOLUME: 0x00090018
- FSCTL_DISMOUNT_VOLUME: 0x00090020

Canonical request shape:

~~~json
{"id":"<request-id>","cmd":"RELEASE_STORAGE","args":{"releaseAttemptId":"<attempt-id>"}}
~~~

Canonical success fields:

~~~text
ok=true
result.releaseAttemptId=<attempt-id>
result.accepted=true
~~~

The release request is non-repeatable within the same attempt. If response delivery is accepted and later serial close/dispose reports an error, preserve the accepted response and evaluate teardown independently.

## 7. Efficient orderly lifecycle cycle

Start from one qualified MSC + CDC baseline.

Recommended cycle:

1. Snapshot baseline manifest count / identity and relevant recording state.
2. Perform exact volume lock + dismount.
3. Send RELEASE_STORAGE exactly once.
4. Validate matching accepted response.
5. Observe complete teardown with a bounded deadline.
6. Physically remove USB.
7. With USB still removed, hold WAKE for 3 seconds, then release it.
8. Allow a recording-evidence dwell before reconnect. A 180-second dwell was a practical stable value during the corrective Human Gate; this is harness timing, not a product requirement.
9. Reconnect USB without pressing WAKE/RESET/BOOT.
10. Wait for qualified MSC + CDC publication.
11. Verify exactly one new finalized manifest entry.
12. Verify WAV exists, matching .part is absent, size matches, SHA-256 matches, duration matches, and recording is non-empty.
13. Count the cycle only after the full required observation is valid.

A reconnect that occurs too soon after WAKE can finalize a 44-byte / 0-ms WAV before useful PCM is captured. Treat that as insufficient fresh-recording evidence, not automatically as a release/teardown defect.

## 8. Efficient accidental pre-release removal cycle

Start from a qualified baseline and capture:

- manifest entry count;
- manifest SHA-256;
- recording-file count;
- deterministic recording-file snapshot hash based on relative filename + size;
- .part count.

Recommended sequence:

1. Do not safe-remove and do not send RELEASE_STORAGE.
2. Physically unplug USB.
3. Keep the Device detached for the bounded matrix interval.
4. Reconnect without WAKE/RESET/BOOT.
5. Wait for qualified re-publication.
6. Prove the manifest hash/count and recording-file snapshot are unchanged.
7. Prove no new .part and no automatic recording/remount occurred.
8. Only after that fail-closed proof, execute the canonical lock + dismount + RELEASE_STORAGE sequence.
9. Observe complete teardown.
10. Physically remove USB.
11. Hold WAKE for 3 seconds while USB remains removed.
12. Use the recording-evidence dwell.
13. Reconnect and obtain the fresh-recording integrity proof.

If volume lock fails before RELEASE_STORAGE is sent, preserve the already-valid accidental-pull snapshot. Close host consumers such as file-browser windows and retry only the lock/dismount/release recovery phase when the snapshot is still unchanged. Do not repeat the earlier physical pull solely to obtain a cleaner wrapper result.

## 9. Failure and recovery decision table

| Observation | Classification / action |
|---|---|
| HEAD mismatch | STOP; no acceptance action |
| Qualified baseline missing | BLOCKED; repair baseline first |
| Volume lock fails and request_sent=false | Host/harness BLOCKED; close host handles and bounded-retry the release phase only |
| Dismount fails and request_sent=false | BLOCKED; do not send RELEASE_STORAGE |
| RELEASE_STORAGE may have been sent | Never resend in that attempt |
| Matching accepted response + complete teardown, but serial close fails | Serial cleanup issue; do not overturn independently valid product evidence |
| Accepted response but teardown does not complete during a valid full observation window | Product failure for the required teardown path; no release retry |
| Manual WAKE was omitted or operator is unsure it occurred | Required physical sequence is invalid; mark the affected cycle NOT_COUNTED / BLOCKED as appropriate, not product FAIL |
| Fresh WAV is 44 bytes / 0 ms after otherwise valid recovery | Fresh-recording evidence insufficient; do not infer release failure; use a longer approved evidence dwell |
| Read-only NVS/flash diagnostic was run | Runtime continuity is broken; establish a fresh application baseline before continuing |
| Wrapper/parser verdict contradicts immutable raw observations | Correct the harness verdict from preserved evidence; do not repeat physical actions solely for formatting |

## 10. Efficiency rules

- Perform environment/toolchain qualification once per host session, not once per physical cycle.
- Perform exact-head build/flash once per exact revision unless the Device firmware or test fixture must actually change.
- After a PC reboot, first re-establish Git/IDF environment and observe the Device baseline. Do not reflexively rebuild/reflash.
- Keep file-browser windows and other consumers closed against the Device volume before lock/dismount phases.
- Use one complete PowerShell packet per cycle with Japanese ACTION= / WAIT= prompts and Enter as the synchronization boundary.
- Keep the cycle number, baseline manifest count, and rolling PASS count in the summary.
- Increase the recording-evidence dwell rather than repeatedly generating invalid 44-byte/0-ms attempts.
- Snapshot before interruption tests so a later host-side BLOCKED recovery can resume without repeating already-valid physical evidence.
- Never convert fixture repair, diagnostic reboot, flash, or baseline recovery into an acceptance cycle.
- Prefer observation-only follow-up after any accepted release.

## 11. Durable evidence fields

For each counted lifecycle cycle, retain at minimum:

~~~text
exact_head
cycle_id
baseline_manifest_count
release_lock_succeeded
release_dismount_succeeded
request_sent
response_accepted
release_retry_count
teardown_observed
physical_usb_removal_confirmed
manual_wake_confirmed
publication_observed
final_manifest_delta
last_state
wav_present
wav_part_absent
size_match
sha256_match
duration_match
nonempty_recording
filesystem_corruption_observed
dual_ownership_observed
silent_success_observed
diagnosis
disposition
~~~

For accidental pre-release cycles additionally retain:

~~~text
baseline_manifest_sha256
baseline_file_snapshot_sha256
post_pull_manifest_sha256
post_pull_file_snapshot_sha256
manifest_unchanged_after_pull
files_unchanged_after_pull
no_part_after_pull
no_automatic_recording_after_pull
~~~

Publish only sanitized structured evidence. Rich local logs stay local unless separately reduced to public-safe evidence.

## 12. Historical qualification from #87

The corrective #87 Human Gate demonstrated that this operating model can produce stable evidence, including:

- a 10-cycle orderly lifecycle corrective matrix;
- a 5-cycle accidental pre-release fail-closed matrix;
- exactly-once canonical release attempts;
- safe recovery from a pre-release Windows volume-lock BLOCKED condition without repeating preserved physical evidence;
- correction of an invalid operator sequence where manual WAKE was uncertain;
- separation of post-response serial cleanup behavior from independently observed release/teardown success.

Those historical results apply only to their exact tested revision. The operational lessons in this runbook are reusable; the acceptance evidence itself is not automatically reusable after a revision change.

## 13. Before the next Human Gate

Before giving the operator any command:

1. read the current Issue / Spec / Decision and determine the exact matrix row;
2. read all required files under .agent/HUMAN_GATE;
3. confirm the current PR HEAD and prerequisite Review/Security/CI state;
4. identify whether the host session needs only environment activation or also build/flash;
5. choose the smallest phase that can produce one unambiguous observation;
6. decide in advance which failures permit observation-only continuation and which require a fresh baseline;
7. serialize only public-safe durable evidence and validate it before publication.
