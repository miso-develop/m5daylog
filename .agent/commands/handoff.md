# /handoff

command_id: handoff
alias: /h
version: 2

## Purpose

Prepare the appropriate handoff from the active Role to the Role that owns the next action.

## Behavior

1. Preserve the current `ACTIVE_ROLE` and `DOMAIN`.
2. Inspect the current durable state needed to identify the next owning Role.
3. Follow `.agent/HANDOFF_PROTOCOL.md` for routing, Domain handling, required content, and completion criteria.
4. Do not perform the target Role's work.
5. Produce a concise, copy-paste-ready instruction for the target Agent chat when human transfer between chats is required.
6. Do not repeat information that the target Agent can obtain from the referenced Issue / PR / Spec; include only context not durably captured there, the exact next action, and the completion condition.
7. If the project workflow requires durable handoff evidence and the active Role is permitted to write it, persist the handoff state in the appropriate GitHub location before reporting completion.

## Safety

- Do not change the current chat's Role implicitly.
- Do not invent a target Domain when the target Role forbids Domain or when an optional-Domain handoff is intentionally cross-domain.
- Do not treat handoff as permission to bypass unresolved Human Gates, dependencies, or required decisions.

## Alias

`/h` is an exact alias of `/handoff` and follows this same contract.
