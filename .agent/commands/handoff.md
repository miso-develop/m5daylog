# /handoff

command_id: handoff
alias: /h
version: 3

## Purpose

Persist the current Role's material state in a visibility-appropriate durable location, then prepare the canonical structural handoff to the Role that owns the next action.

## Behavior

1. Preserve the current `ACTIVE_ROLE` and `DOMAIN`.
2. Inspect current durable state and identify the next owning Role.
3. Follow `.agent/HANDOFF_PROTOCOL.md` for routing, Domain handling, field rules, revision identity, repository visibility boundaries, and state vocabulary.
4. Verify that all material findings, evidence, decisions, limitations, and next-role requirements are already persisted in an appropriate durable location consistent with repository visibility and confidentiality. If they are not, persist them first when the active Role is permitted to do so; otherwise report the durable-state deficiency instead of hiding it in the handoff.
5. Do not perform the target Role's work.
6. When human transfer between Agent chats is required, produce a copy-paste-ready handoff using only the canonical structural fields. Do not append narrative context that the receiver can reconstruct from GitHub.
7. When the workflow records a durable handoff event in GitHub and the active Role is permitted to write it, use the same canonical structure as the chat handoff.
8. For PR-based transitions, include the exact current PR `HEAD` as required by the Handoff Protocol.

## Safety

- Do not change the current chat's Role implicitly.
- Do not invent a target Domain when the target Role forbids Domain or when an optional-Domain handoff is intentionally cross-domain.
- Do not treat handoff as permission to bypass unresolved Human Gates, dependencies, security findings, required decisions, or stale revision state.
- Never use chat-only handoff prose as the sole record of material project state.
- Never disclose private-repository names, URLs, paths, Issue/PR identifiers, branch names, internal artifact names, or other private-source metadata into a public repository handoff without explicit human approval.
- If private traceability is required for a public workflow, keep that traceability in an approved private durable location and expose only the public-safe transition in the public repository.

## Output shape

Example:

```text
HANDOFF
FROM: implementation
TO: review
DOMAIN: device
ISSUE: #139
PR: #143
HEAD: <full-pr-head-sha>
STATE: READY_FOR_REVIEW
```

Fields that are not applicable under `.agent/HANDOFF_PROTOCOL.md` are omitted rather than filled with narrative placeholders.

## Alias

`/h` is an exact alias of `/handoff` and follows this same contract.
