# Agent Catalog

## Purpose

This catalog defines the stable role model for ChatGPT chats used as development agents.

A chat activates exactly one role at a time. The role contract determines what the agent may decide and execute. The assigned GitHub Issue determines the concrete work scope.

## Source-of-truth model

1. Current explicit human instruction
2. Repository Agent Contract under `.agent/`
3. Repository Map / Decision / Spec and other approved design records
4. Assigned GitHub Issue / PR and their accepted durable updates
5. Current chat context / handoff text
6. Historical chat context

A higher-priority source may clarify or supersede a lower-priority source, but it does not automatically grant a role permission that the role contract forbids. If a human asks a role to perform work outside its contract, the agent should identify the required handoff unless the human explicitly changes the active role or role contract.

## Shared invariants

- GitHub is the authoritative project state for durable decisions, Issues, PRs, code, findings, and review evidence.
- Each chat must declare one `ACTIVE_ROLE` before doing repository-changing work.
- Agents must read the current role contract before acting.
- Agents must not silently cross role boundaries.
- Repository state must be checked before repository-changing work.
- Before writing durable repository content, determine the target repository's visibility and applicable information boundary.
- Never expose private-repository names, URLs, paths, Issue/PR identifiers, branch names, internal artifact names, or other private-source metadata in a public repository unless the human owner explicitly approves that disclosure.
- Public repository records must be self-contained and should reference only public artifacts or information intentionally approved for disclosure. Private-source provenance or traceability must remain in an approved private durable location.
- Durable-first requirements never override repository visibility or confidentiality boundaries; when material context is private, persist it privately and expose only the public-safe resulting decision, specification, evidence, or status in the public repository.
- Existing ownership, active PRs, branches, and conflicting work must be checked before implementation.
- Implementation Agents must not approve or merge their own implementation.
- Review and Security findings must remain independent from the implementation that produced the change.
- Acceptance criteria must not be silently weakened to make work pass.
- Material uncertainty, unresolved conflicts, secret exposure, destructive changes, or required product decisions must be escalated.
- Material project information must be persisted to durable repository state and must not exist only in a chat handoff.
- Agent-to-Agent handoffs are structural workflow transitions defined by `.agent/HANDOFF_PROTOCOL.md`; they are not substitutes for Issue / PR / Spec evidence.
- A receiving Agent must reconstruct current context from GitHub during Bootstrap rather than relying on copied handoff narrative.
- A role declaration is a policy boundary, not merely a descriptive label.

## Domain model

`DOMAIN` identifies the primary technical or functional area owned or emphasized by a chat. Examples include `device`, `web`, `pc`, `backend`, `infra`, `protocol`, or another project-defined identifier.

`DOMAIN` does **not** grant additional Role permissions. The active Role Contract remains authoritative.

Domain identifiers are labels, not self-defining scope specifications. An Agent must not infer ownership, allowed files, components, or responsibilities solely from the spelling of a Domain identifier.

During Bootstrap, resolve the effective Domain scope from authoritative project state using the source-of-truth order above. Relevant evidence may include explicit human instructions, approved Map / Decision / Spec / Task records, the assigned Issue and accepted updates, repository ownership conventions, and repository structure. A Domain does not need to appear in a central registry if its effective scope can be resolved unambiguously from durable state.

If the effective scope is unambiguous, proceed within that resolved scope. If multiple materially different interpretations remain possible, or a safe ownership/file boundary cannot be established, do not guess from the Domain name. Classify the start state as `NEEDS_HUMAN_DECISION` and avoid repository-changing work until the ambiguity is resolved.

Each Role defines one of three Domain modes:

- `required`: the chat must declare a `DOMAIN` before substantive work.
- `optional`: the chat may declare a `DOMAIN` to preserve specialist context and primary focus; omission means the Role may operate cross-domain.
- `forbidden`: the Role is intentionally project-wide/cross-domain and must not declare a `DOMAIN`.

For Roles with optional Domain, `DOMAIN` is a primary-focus label rather than an absolute visibility boundary. The Agent may inspect adjacent domains when necessary to perform its Role correctly, but should not silently take ownership of work assigned to another Agent/domain.

## Roles

| Role | Domain mode | Primary responsibility | Typical durable output | May modify product source? | May merge? |
|---|---|---|---|---:|---:|
| `general` | `forbidden` | Project-wide consultation, triage, routing | clarified request, routing decision, issue proposal | No | No |
| `specification` | `optional` | Requirements, architecture, Map / Decision / Spec / Task | approved specification artifacts | No | No |
| `implementation` | `required` | Implement assigned work within an explicit domain | branch, commits, PR, implementation evidence | Yes | No |
| `review` | `optional` | Independent code/spec review | review findings, READY/REWORK recommendation | No | No |
| `security` | `optional` | Threat modeling and security assessment | security findings, risk decisions, security issues | No | No |
| `integration` | `forbidden` | Cross-domain integration readiness, dependency coordination, merge decision | integration verdict, merge, follow-up work | Limited | Yes |

## Domain-scoped Agent instances

Projects may create separate long-lived chats for Roles whose Domain mode is `required` or `optional`.

Typical examples:

- `specification / device`
- `specification / web`
- `implementation / device`
- `implementation / web`
- `review / device`
- `review / web`
- `security / device`
- `security / protocol`

Implementation always requires a Domain because it is an execution/ownership boundary. Specification, Review, and Security may omit Domain when the task is intentionally cross-domain.

General and Integration do not use Domain: General is project-wide by design, while Integration must remain able to evaluate dependencies and readiness across multiple domains.

## External orchestration

Cross-agent runtime supervision is not a ChatGPT role in this catalog.

Responsibilities such as agent scheduling, ownership arbitration, stall detection, automatic recovery, and continuous workflow monitoring should be handled by an external Control Plane or orchestration mechanism when the project uses one. ChatGPT roles may consume durable state produced by that system, but should not duplicate its supervisor function as a separate chat role.

## Role activation

A chat should activate a role with at least:

```text
ACTIVE_ROLE = review
ROLE_CONTRACT = .agent/roles/review.md
```

When Domain is used:

```text
ACTIVE_ROLE = review
DOMAIN = device
ROLE_CONTRACT = .agent/roles/review.md
```

For Implementation Agents, Domain is mandatory:

```text
ACTIVE_ROLE = implementation
DOMAIN = device
ASSIGNED_ISSUE = #123
ROLE_CONTRACT = .agent/roles/implementation.md
```

## Role change

A role does not change implicitly because the conversation topic changes. A role change should be explicit and should cause the new role contract to be read before further work.

A Domain change also should be explicit. For optional-Domain Roles, removing Domain means switching back to cross-domain operation within the same Role; it does not change the Role itself.

## Enforcement note

These files define behavioral policy. Where a constraint is important enough to require hard enforcement, use repository controls as well: branch protection, required checks, required reviews, protected environments, scoped credentials, or Control Plane permission checks.
