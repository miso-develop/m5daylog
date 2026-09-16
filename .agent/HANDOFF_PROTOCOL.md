# Handoff Protocol

## Purpose

A handoff transfers responsibility without requiring another agent to reconstruct the task from chat history.

## Handoff triggers

Use a handoff when:

- the next action is forbidden by the active role;
- another role owns the required decision;
- implementation requires a specification change;
- review finds rework;
- integration finds unresolved implementation or test work;
- a security finding requires remediation by an Implementation Agent;
- a dependency or ownership conflict blocks progress;
- a human decision or Human Gate is required.

Do not silently perform cross-role work merely because it is small.

## Domain-aware routing

When the target Role has `domain_mode: required`, the handoff must identify the target `DOMAIN`.

When the target Role has `domain_mode: optional`, include `DOMAIN` when a specialist owner/focus is known and useful. Omit it when the target work is intentionally cross-domain.

When the target Role has `domain_mode: forbidden`, do not assign a `DOMAIN`.

A Domain declaration does not grant permissions beyond the target Role Contract.

## Required handoff content

A durable handoff should identify:

- source role
- target role
- target Domain when required or useful under the target Role's Domain mode
- Issue / PR
- current state
- concise finding or reason
- evidence
- exact requested next action
- blocking/non-blocking status
- relevant acceptance criteria or specification references

Recommended format for a domain-targeted handoff:

```markdown
## HANDOFF

FROM: review
TO: implementation
DOMAIN: device
ISSUE: #123
PR: #130
STATE: REWORK_REQUIRED
BLOCKING: yes

### Finding
<concise finding>

### Evidence
<files/tests/logs/spec references>

### Required action
<what the target role must do>

### Completion condition
<observable condition for handing back>
```

For optional-Domain Roles, omit `DOMAIN` when the work is deliberately cross-domain.

## Standard handoff routes

| From | Condition | To |
|---|---|---|
| General | requirement needs formalization | Specification |
| Specification | implementation-ready task | Implementation |
| Implementation | implementation complete | Review |
| Review | rework required | Implementation |
| Review | review passed | Integration |
| Any | security concern | Security |
| Security | remediation required | Implementation |
| Integration | implementation defect | Implementation |
| Integration | specification ambiguity | Specification |
| Integration | human/physical verification required | Human Gate |
| Any | unresolved ownership/runtime coordination problem | Human or external Control Plane |

## Status vocabulary

Prefer a small stable vocabulary:

- `READY_FOR_IMPLEMENTATION`
- `READY_FOR_REVIEW`
- `REWORK_REQUIRED`
- `READY_FOR_INTEGRATION`
- `BLOCKED`
- `NEEDS_SECURITY_REVIEW`
- `NEEDS_HUMAN_DECISION`
- `HUMAN_GATE_REQUIRED`
- `INTEGRATED`

## Handoff completion

The receiving role should verify current repository state before acting. A handoff is context, not permission to ignore newer GitHub state.

When work is completed, the receiving role should leave durable evidence in the Issue/PR and hand responsibility to the next role rather than relying only on a chat message.
