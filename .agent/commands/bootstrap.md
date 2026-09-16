# /bootstrap

command_id: bootstrap
alias: /b
version: 2

## Purpose

Re-run Agent bootstrap for the current chat without changing its assigned Role or Domain.

## Behavior

1. Preserve the current `ACTIVE_ROLE` and `DOMAIN`.
2. Re-read `.agent/AGENT_CATALOG.md`.
3. Re-read `.agent/roles/<ACTIVE_ROLE>.md`.
4. Re-read `.agent/BOOTSTRAP.md` and `.agent/HANDOFF_PROTOCOL.md` as required by that protocol.
5. Re-inspect current repository state required by bootstrap.
6. Reclassify readiness using the bootstrap status vocabulary.
7. Emit the normal concise startup report.

## Safety

- Do not infer a different Role from the work requested in chat.
- Do not use this command to bypass an invalid Domain assignment or Role boundary.
- Do not automatically continue substantive work after bootstrap unless the human request also asks for continuation or another command requires it.

## Alias

`/b` is an exact alias of `/bootstrap` and follows this same contract.
