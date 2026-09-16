# Handoff Protocol

version: 3

## Purpose

A handoff is a **workflow ownership/state transition notification**, not a context-transfer document.

The repository is the durable source of truth. The receiving Agent reconstructs the current working context from the referenced GitHub Issue / PR / Spec / checks during Bootstrap rather than relying on copied chat context.

## Core invariant: durable first

Material information MUST NOT exist only in a handoff message.

Before emitting a handoff, the source Agent must persist all material information needed by the next Role to the appropriate durable repository location, including as applicable:

- requirements, Acceptance Criteria, scope, dependencies, and specification decisions;
- implementation summary and changed behavior;
- test / CI evidence and verification results;
- review or security findings and their supporting evidence;
- known limitations and unresolved risks;
- Human Gate requirements/results;
- integration findings or decisions.

Use the relevant Issue, PR, Spec / Decision artifact, checks, or another repository-approved durable record.

The handoff MUST NOT duplicate information that the receiving Agent can retrieve from those durable sources.

## Repository visibility boundary

Durable-first does not override repository visibility or confidentiality boundaries.

Before persisting material state or a durable handoff record, determine whether the target repository/location is public or private.

For a public repository:

- do not expose private-repository names, URLs, paths, Issue/PR identifiers, branch names, internal artifact names, or other private-source metadata unless the human owner explicitly approves disclosure;
- when work is derived from a private source, persist only the public-safe resulting decision, specification, evidence, or status;
- keep private provenance/traceability in an approved private durable location;
- make public Issue / PR / comment / commit / documentation content self-contained using public artifacts or intentionally disclosed information.

If material context cannot be safely persisted in the public repository, persist it in an approved private durable location and put only the minimum public-safe state needed for workflow execution in the public repository. Do not compensate by placing private details in the chat handoff.

## Handoff triggers

Use a handoff when:

- the next action is forbidden by the active Role;
- another Role owns the required decision or work;
- implementation requires a specification change;
- Review finds rework or clears work for Integration;
- Integration finds unresolved implementation, specification, security, dependency, or Human Gate work;
- Security requires remediation or another Role's decision;
- a dependency or ownership conflict blocks progress;
- a human decision or Human Gate is required.

Do not silently perform cross-role work merely because it is small.

## Canonical handoff schema

Agent-to-Agent handoffs should contain only the structural transition fields below.

```text
HANDOFF
FROM: implementation
TO: review
DOMAIN: web
ISSUE: #146
PR: #147
HEAD: 33c555696445637043a5e2858c42a23a1858d01a
STATE: READY_FOR_REVIEW
```

Do not append `Finding`, `Evidence`, `Required action`, `Completion condition`, implementation summaries, test summaries, or other narrative sections when that information is durably available in GitHub.

The same canonical structure should be used for both:

1. the durable handoff record in GitHub when the workflow records one; and
2. the copy-paste-ready handoff message sent to another Agent chat.

This avoids separate GitHub and chat representations of the same transition.

## Field rules

- `FROM`: required. Source Role.
- `TO`: required. Target Role or `human` for an explicit Human Gate / human decision transition.
- `DOMAIN`: conditional. Include when the target Role requires Domain, or when an optional-Domain target has an explicit specialist owner/focus. Omit when the target Role forbids Domain or the target work is intentionally cross-domain.
- `ISSUE`: required for normal repository work. It identifies the durable work item from which the receiver reconstructs context.
- `PR`: required when the transition concerns an implementation/review/integration PR; otherwise omit it.
- `HEAD`: required when `PR` is present and the transition is tied to a specific PR revision. Use the full commit SHA when available.
- `STATE`: required. Use the stable workflow vocabulary defined by the Role/Protocol.

Do not add a separate `BLOCKING` field when blocking semantics are already represented by `STATE`; avoiding redundant fields prevents contradictory transitions.

## Domain-aware routing

When the target Role has `domain_mode: required`, the handoff must identify the target `DOMAIN`.

When the target Role has `domain_mode: optional`, include `DOMAIN` when a specialist owner/focus is known and useful. Omit it when the target work is deliberately cross-domain.

When the target Role has `domain_mode: forbidden`, do not include `DOMAIN`.

A Domain declaration never grants permissions beyond the target Role Contract.

## Revision identity and stale handoffs

For PR-based transitions, `HEAD` identifies the exact revision that entered the stated workflow state.

The receiving Agent must compare `HEAD` with the current PR head during Bootstrap. If they differ, the handoff is stale. Do not assume that the old transition still applies; inspect the latest durable Issue / PR / review / checks and derive the current disposition from GitHub.

A handoff is never authority to ignore newer repository state.

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
| Integration | human/physical verification required | Human |
| Any | unresolved ownership/runtime coordination problem | Human or external Control Plane |

## Status vocabulary

Prefer a small stable vocabulary. Role Contracts may define role-specific states; common transition states include:

- `READY_FOR_IMPLEMENTATION`
- `READY_FOR_REVIEW`
- `REWORK_REQUIRED`
- `READY_FOR_INTEGRATION`
- `BLOCKED`
- `BLOCKED_DEPENDENCY`
- `BLOCKED_CONFLICT`
- `SPEC_CHANGE_REQUIRED`
- `NEEDS_SECURITY_REVIEW`
- `NEEDS_HUMAN_DECISION`
- `HUMAN_GATE_REQUIRED`
- `INTEGRATED`

## Completion rule

A Role may emit a handoff only after the material state supporting that transition has been persisted durably in a location appropriate for its visibility/sensitivity.

The handoff itself is intentionally insufficient to perform the target Role's work. The receiver must Bootstrap and reconstruct current context from GitHub.
