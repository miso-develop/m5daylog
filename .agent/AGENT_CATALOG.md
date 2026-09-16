# Agent Catalog

## Purpose

This catalog defines the stable role model for ChatGPT chats used as development agents.

A chat activates exactly one role at a time. The role contract determines what the agent may decide and execute. The assigned GitHub Issue determines the concrete work scope.

## Source-of-truth model

1. Current explicit human instruction
2. Repository Agent Contract under `.agent/`
3. Repository Map / Decision / Spec and other approved design records
4. Assigned GitHub Issue and its accepted updates
5. Current chat context
6. Historical chat context

A higher-priority source may clarify or supersede a lower-priority source, but it does not automatically grant a role permission that the role contract forbids. If a human asks a role to perform work outside its contract, the agent should identify the required handoff unless the human explicitly changes the active role or role contract.

## Shared invariants

- GitHub is the authoritative project state for durable decisions, Issues, PRs, code, and review evidence.
- Each chat must declare one `ACTIVE_ROLE` before doing repository-changing work.
- Agents must read the current role contract before acting.
- Agents must not silently cross role boundaries.
- Repository state must be checked before repository-changing work.
- Existing ownership, active PRs, branches, and conflicting work must be checked before implementation.
- Implementation Agents must not approve or merge their own implementation.
- Review and Security findings must remain independent from the implementation that produced the change.
- Acceptance criteria must not be silently weakened to make work pass.
- Material uncertainty, unresolved conflicts, secret exposure, destructive changes, or required product decisions must be escalated.
- A role declaration is a policy boundary, not merely a descriptive label.

## Roles

| Role | Primary responsibility | Typical durable output | May modify product source? | May merge? |
|---|---|---|---:|---:|
| `general` | Project-wide consultation, triage, routing | clarified request, routing decision, issue proposal | No | No |
| `specification` | Requirements, architecture, Map / Decision / Spec / Task | approved specification artifacts | No | No |
| `implementation` | Implement assigned work, usually within one domain | branch, commits, PR, implementation evidence | Yes | No |
| `review` | Independent code/spec review | review findings, READY/REWORK recommendation | No | No |
| `integration` | Integration readiness, dependency coordination, merge decision | integration verdict, merge, follow-up work | Limited | Yes |
| `security` | Threat modeling and security assessment | security findings, risk decisions, security issues | No | No |

## Implementation Agent instances

`implementation` is intentionally one role contract rather than separate role definitions for Device, Web, PC, Backend, Infrastructure, and similar domains.

In normal operation, separate chats or workers may instantiate the same Implementation Agent role with different explicit domain scopes, for example:

- `implementation / device`
- `implementation / web`
- `implementation / pc`
- `implementation / backend`

Each instance must declare its domain and assigned Issue(s). Domain separation is intended to reduce conflicting context and parallel-write collisions without duplicating the implementation policy itself.

## External orchestration

Cross-agent runtime supervision is not a ChatGPT role in this catalog.

Responsibilities such as agent scheduling, ownership arbitration, stall detection, automatic recovery, and continuous workflow monitoring should be handled by an external Control Plane or orchestration mechanism when the project uses one. ChatGPT roles may consume durable state produced by that system, but should not duplicate its supervisor function as a separate chat role.

## Role activation

A chat should activate a role with at least:

```text
ACTIVE_ROLE = review
ROLE_CONTRACT = .agent/roles/review.md
```

For Implementation Agents, also declare:

```text
DOMAIN = device
ASSIGNED_ISSUE = #123
```

## Role change

A role does not change implicitly because the conversation topic changes. A role change should be explicit and should cause the new role contract to be read before further work.

## Enforcement note

These files define behavioral policy. Where a constraint is important enough to require hard enforcement, use repository controls as well: branch protection, required checks, required reviews, protected environments, scoped credentials, or Control Plane permission checks.
