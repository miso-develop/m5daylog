# HumanGate — reusable operator scripts

Project-specific, versioned scripts and knowledge; normative authority stays with .agent/HUMAN_GATE rules, approved Spec/Decision/Acceptance Criteria and current Issue/PR. Historical success evidence is NOT acceptance for a new revision.

## Per-worktree setup

From Windows PowerShell 5.1 in the target worktree:

    & { & .\HumanGate\init.ps1 }

Creates the worktree-root ignored .env from HumanGate/.env.example using create-new/no-overwrite semantics. Fill it locally; never paste local values into GitHub/chat/log output. Every device script also creates the empty skeleton if needed then stops with ENV_CREATED_NEEDS_CONFIGURATION. Initialization performs no Device access.

Allowlisted HG_* fields are loaded into the current PowerShell process without evaluating code or expanding expressions. Required for Device observations: HG_EXPECTED_HEAD (40-digit SHA), HG_PYTHON (activated managed interpreter), HG_SERIAL_PORT; HG_NVS_OFFSET/HG_NVS_SIZE must be separately qualified from the exact-head partition table. Do not assume NVS offsets or override existing Device identity. MAC and PnP fields are reserved for destructive-target checks; they do not alone authorize flashing.

Private output is under ignored <worktree>/.local/HumanGate/. Device/host identifiers, full NVS bytes, local absolute paths, and raw audio are not printed.

## Scripts

| Script | Purpose | Mutation |
| --- | --- | --- |
| init.ps1 | Create ignored .env skeleton | Host only |
| check-head.ps1 | Exact HEAD, tracked-tree and interpreter preflight | Host only |
| rom-flash-id.ps1 | Bounded ROM flash_id link check | Persistently read-only; may reset/re-enumerate |
| cdc-readonly.ps1 | GET_STATUS, PING, GET_INFO, GET_STATUS | Read-only CDC commands |
| nvs-read.ps1 | Qualified read_flash + SHA256 local capture | Persistent flash read only; may reset the runtime |

Executable scripts have one outer & { ... } scope. They print periodic PROGRESS for long operations; the FINAL result block starts with a blank line and ==== $phase ====, ends in SUMMARY, and is followed by a blank line. One-phase PASS means only that the stated command completed, not physical AC acceptance.

No saved script sends SET_TIME, RELEASE_STORAGE, erases flash, writes NVS or counts a physical cycle. Those mutating, one-shot and device-ownership operations require separate accepted preconditions and Human Gate; never copy an old successful release request into a generic retry loop. These new wrappers are documentation/implementation candidates, NOT confirmed Windows or Device runtime success.

## Procedure

1. Reconstruct current exact Issue/PR/Spec/Decision. Run init.ps1, fill .env, run check-head.ps1.
2. Confirm Device mode, exact target and no competing CDC consumer before cdc-readonly.ps1. Do not probe after RELEASE_STORAGE accepted or on an ambiguous session.
3. For nvs-read.ps1, establish an approved ROM-mode baseline and exact NVS partition layout. After any esptool diagnostic, re-establish runtime baseline before acceptance.
4. Preserve .local/HumanGate evidence on an error; distinguish harness BLOCKED from product FAIL.
5. Follow KNOWHOW.md and .agent/HUMAN_GATE/M5DAYLOG_DEVICE_RUNBOOK.md. Public evidence must be sanitized and validated before publication.

## Traceability

Sources: #87 sanitized successful command record (issue comment 5991296631) and D-031 operational findings; #50 3/3 RTC recovery and the read-only CDC four-command addendum (issue comments 6097299823 and 6097494303); #51 EV-011 interrupted partial/resync/idempotency is PC-owned, so no cross-domain PC mutation script was added here.
