# Command Catalog

Repository-defined pseudo commands for ChatGPT Agent chats.

This file is a routing catalog. Detailed behavior lives in the matching contract under `.agent/commands/`; load only the contract for the invoked command.

Canonical commands and their short aliases resolve to the same contract.

| Command | Short | Contract | Purpose |
|---|---|---|---|
| `/continue` | `/c` | `.agent/commands/continue.md` | Recover the immediately preceding interrupted ChatGPT task and continue it |
| `/resume` | `/r` | `.agent/commands/resume.md` | Refresh durable project state and resume actionable work for the current Role / Domain |
| `/status` | `/s` | `.agent/commands/status.md` | Refresh authoritative state and report current work status without advancing it |
| `/bootstrap` | `/b` | `.agent/commands/bootstrap.md` | Re-run the Agent bootstrap for the current Role and Domain |
| `/handoff` | `/h` | `.agent/commands/handoff.md` | Prepare the appropriate cross-role handoff |

Short aliases are exact aliases of their canonical commands; they do not define separate behavior.

Pseudo commands do not grant permissions. The current explicit human instruction, active Role Contract, Domain policy, approved specification, and repository safety rules remain authoritative.
