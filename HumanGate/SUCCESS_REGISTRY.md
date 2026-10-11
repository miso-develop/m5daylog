# Historical successful Human Gate command registry

Each row distinguishes a **historical successful operation** from a **newly authored reusable wrapper**. The latter MUST undergo independent Windows/Device validation at its own current exact HEAD before being marked as exercised. Do not transfer old physical AC verdicts.

| Operation / source | Historically verified | Reusable candidate | Current wrapper independently executed? |
| --- | --- | --- | --- |
| Exact-HEAD check, #87 issue comment 5991296631 | Successful local Git revision verification at recorded #87 revision | check-head.ps1 | NO |
| ESP32-S3 ROM flash_id, #87 comment 5991296631 | Read-only bootloader link PASS in qualified download mode | rom-flash-id.ps1 | NO |
| NVS read_flash + SHA-256, #87 comment 5991296631 | Partition read / fixture read-back hash PASS | nvs-read.ps1 | NO |
| Device-ID-only NVS fixture restore, #87 comments 5991296631 and 6017499506 | Human-authorized write+identical readback, zero counted cycles | **No generic mutating script**: use an exact-Human-authorized setup packet | NO |
| D-031 lock+dismount+one RELEASE_STORAGE, #87 comment 6031923682 | Qualified 10/10 orderly cycles at its historical HEAD | Operational sequence in .agent/HUMAN_GATE/M5DAYLOG_DEVICE_RUNBOOK.md, not an automatic retry command | NO |
| Read-only GET_STATUS→PING→GET_INFO→GET_STATUS, #50 comment 6097494303 | Physical four-command addendum PASS, SET_TIME evidence carried from earlier one-shot cycles | cdc-readonly.ps1 | NO |
| SET_TIME / NVS pending / release / manual WAKE / exactly-once RTC event, #50 comment 6097299823 | Qualified 3/3 physical cycles at historical exact HEAD | **No automatic mutation script**: run accepted exact-session controlled Human Gate | NO |
| Interrupted .partial / resync / SQLite idempotency, #51 EV-011 operator log | PC-owned interrupted-sync recovery recorded in human operator output | **No Device-side script**: defer to PC domain #51 | NO |

## Promotion rule for future scripts

After a real successful phase, record (1) exact Git HEAD; (2) exact script blob SHA; (3) PowerShell version; (4) Device/host mode and prerequisites, sanitized; (5) underlying native exit and output disposition; (6) any bounded timeout/cleanup behavior; (7) evidence in approved Issue/PR; (8) explicit script status validated or needs-rework. Keep raw output and local env only under ignored .local/HumanGate, not in this registry. Review/Security/Human Gates remain independent.

If a failed/ambiguous release, flash or NVS mutation was attempted, there must be no automated retry; preserve evidence and require new approved state assessment.
