# HumanGate — reusable operator scripts

Project-specific, versioned scripts and knowledge; normative authority stays with .agent/HUMAN_GATE rules, approved Spec/Decision/Acceptance Criteria and current Issue/PR. Historical success evidence is NOT acceptance for a new revision.

## Per-worktree setup

From Windows PowerShell 5.1 in the target worktree:

    & { & .\HumanGate\init.ps1 }

Creates the worktree-root ignored .env from HumanGate/.env.example using create-new/no-overwrite semantics. Fill it locally; never paste local values into GitHub/chat/log output. Every device script also creates the empty skeleton if needed then stops with ENV_CREATED_NEEDS_CONFIGURATION. Initialization performs no Device access.

Allowlisted HG_* fields are loaded without evaluating code. Every key in .env.example must exist (blank optional values are allowed); missing/duplicate/unknown keys block the command. Import clears all allowlisted HG_* Process variables before parsing so old worktree/shell values cannot fill an omitted field. Older ignored .env files must be updated manually with the two new blank NVS digest keys; init.ps1 never overwrites them.

Required for observations: HG_EXPECTED_HEAD (full SHA), HG_PYTHON and HG_SERIAL_PORT. For nvs-read.ps1, configure HG_NVS_OFFSET and HG_NVS_SIZE **and** two digests copied only from the **same exact-HEAD passing HG-BUILD**: HG_NVS_PLAN_SHA256 (flasher_args_sha256) and HG_NVS_PARTITION_SHA256 (SHA-256 of its generated partition_table/partition-table.bin, verified against the approved flash image set). The helper verifies the fixed worktree-relative firmware/build/hg-qualified plan/table, the plan's partition image reference, unique binary type=data/subtype=nvs/label=nvs, and exact NVS offset/size before touching a Device. Hashes or plan from another revision are not acceptable. A local artifact match is not itself proof of which image the Device runs; independently qualify the Device baseline. MAC/PnP fields alone do not authorize flashing.

Private output is under ignored <worktree>/.local/HumanGate/. Device/host identifiers, full NVS bytes, local absolute paths, and raw audio are not printed.

## Scripts

| Script | Purpose | Mutation |
| --- | --- | --- |
| init.ps1 | Create ignored .env skeleton | Host only |
| verify-scripts.ps1 | Parse every saved PowerShell script with the Windows PowerShell parser | Host only |
| test-safety.ps1 | Same-shell env, partition and mock-child negative fixtures | Host only |
| check-head.ps1 | Exact HEAD, tracked-tree and interpreter preflight | Host only |
| rom-flash-id.ps1 | Bounded ROM flash_id link check | Persistently read-only; may reset/re-enumerate |
| cdc-readonly.ps1 | GET_STATUS, PING, GET_INFO, GET_STATUS | Read-only CDC commands |
| nvs-read.ps1 | Qualified read_flash + SHA256 local capture | Persistent flash read only; may reset the runtime |

Executable scripts have one outer & { ... } scope. They print periodic PROGRESS for long operations; the FINAL result block starts with a blank line and ==== $phase ====, ends in SUMMARY, and is followed by a blank line. One-phase PASS means only that the stated command completed, not physical AC acceptance.

No saved script sends SET_TIME, RELEASE_STORAGE, erases flash, writes NVS or counts a physical cycle. Those mutating, one-shot and device-ownership operations require separate accepted preconditions and Human Gate; never copy an old successful release request into a generic retry loop. These new wrappers are documentation/implementation candidates, NOT confirmed Windows or Device runtime success.

## Procedure

1. Reconstruct current exact Issue/PR/Spec/Decision. Run verify-scripts.ps1 to catch syntax errors without Device access; run init.ps1, fill .env, then run check-head.ps1.
2. Confirm Device mode, exact target and no competing CDC consumer before cdc-readonly.ps1. Do not probe after RELEASE_STORAGE accepted or on an ambiguous session.
3. For nvs-read.ps1, verify exact-head HG-BUILD plan + partition image digests and the correct ROM baseline before invoking the read. A missing/mismatched hash or wrong in-bounds region blocks before Device access. Re-establish runtime baseline afterward.
4. After a timeout, cleanup error or unknown child state, preserve the private .local/HumanGate/*.status.txt receipt (PID, deadline, root-exit and tree state). Independently prove process-tree absence before any operator-approved retry; never automatically retry or publish local PIDs.
5. Run test-safety.ps1 on Windows PowerShell 5.1 without a Device; its host-only negatives are NOT physical acceptance.
6. Preserve .local/HumanGate evidence on an error; distinguish harness BLOCKED from product FAIL.
7. Follow KNOWHOW.md, SUCCESS_REGISTRY.md and .agent/HUMAN_GATE/M5DAYLOG_DEVICE_RUNBOOK.md. Public evidence must be sanitized and validated before publication.

## Traceability

Sources: #87 sanitized successful command record (issue comment 5991296631) and D-031 operational findings; #50 3/3 RTC recovery and the read-only CDC four-command addendum (issue comments 6097299823 and 6097494303); #51 EV-011 interrupted partial/resync/idempotency is PC-owned, so no cross-domain PC mutation script was added here.
