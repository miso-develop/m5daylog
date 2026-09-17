# Agent Bootstrap Protocol

version: 3

Every Agent chat must perform this bootstrap before substantive repository work.

## 1. Identify execution context

Confirm:

- `ACTIVE_ROLE`
- Role Contract path
- project/repository
- target repository visibility (`public` / `private`) before any write
- assigned Issue(s), if any
- referenced PR / handoff, if any
- `DOMAIN`, according to the active Role's Domain mode

Domain modes are defined in `.agent/AGENT_CATALOG.md` and `.agent/roles.yaml`:

- `required`: `DOMAIN` must be present.
- `optional`: `DOMAIN` may be present; omission means cross-domain operation within the same Role.
- `forbidden`: `DOMAIN` must not be present.

When `DOMAIN` is present, resolve its effective technical/functional scope before substantive work. The Domain identifier is a label, not a self-defining file or ownership boundary. Do not infer scope solely from the Domain name.

Resolve Domain scope from authoritative project state in priority order, including as relevant:

1. explicit human instruction;
2. repository Agent Contract;
3. approved Map / Decision / Spec / Task records;
4. assigned Issue and accepted updates;
5. repository ownership conventions and current repository structure.

A Domain does not need to be pre-registered if these sources establish one unambiguous effective scope. If multiple materially different interpretations remain possible, or a safe ownership/file boundary cannot be established, classify the start state as `NEEDS_HUMAN_DECISION` and do not perform repository-changing work until the Domain scope is clarified.

If the Role is missing or ambiguous, do not infer a privileged Role from the requested action. If Domain usage conflicts with the active Role's Domain mode, classify the start state as `BLOCKED_ROLE_BOUNDARY` until activation is corrected.

## 2. Load policy

Read:

1. `.agent/AGENT_CATALOG.md`
2. `.agent/roles/<ACTIVE_ROLE>.md`
3. `.agent/HANDOFF_PROTOCOL.md`
4. relevant Map / Decision / Spec / Task records

For long-running chats, re-read the Role Contract only after recovery, an explicit Role change, or when repository state indicates the contract changed.

## 3. Reconstruct context from a handoff

When the chat is started or resumed from a handoff:

1. Parse the structural handoff fields only: `FROM`, `TO`, optional `DOMAIN`, `ISSUE`, optional `PR`, optional `HEAD`, and `STATE`.
2. Treat the handoff as a transition pointer, not as project evidence.
3. Fetch the referenced Issue and its latest durable comments/state.
4. Fetch the referenced PR when present, including the current head, diff/review state, and relevant checks/evidence.
5. If `HEAD` is present, compare it with the current PR head.
6. Read relevant approved Spec / Decision / Acceptance Criteria and any referenced durable findings.
7. Derive the current actionable state from GitHub rather than from stale chat history.

If the current PR head differs from `HANDOFF.HEAD`, classify the handoff as stale. Do not assume the original transition still applies. Inspect the latest durable state and determine the current disposition before acting.

Material context that exists only in a chat handoff must not be treated as a substitute for repository evidence. If required material information is missing from durable state, record or request that deficiency rather than relying on transient chat text.

## 4. Inspect current repository state and information boundary

Before repository-changing work, inspect the relevant current state:

- repository visibility (`public` or `private`);
- assigned Issue and recent comments;
- related PRs and exact current head;
- active branches or ownership markers when available;
- prerequisite Issues / PRs;
- CI/check status when relevant;
- durable Review / Security / Human Gate findings;
- recent changes that may invalidate the task context.

Before any GitHub write, apply the repository visibility boundary defined by `.agent/AGENT_CATALOG.md`:

- do not copy private-repository names, URLs, paths, Issue/PR identifiers, branch names, internal artifact names, or other private-source metadata into a public repository without explicit human approval;
- when public work is derived from a private source, write only the public-safe resulting decision, specification, evidence, or status to the public repository;
- keep private provenance/traceability in an approved private durable location;
- durable-first does not mean that private material should be moved into a public repository.

Prefer current GitHub state over handoff text, chat summaries, or historical context.

## 5. Check ownership and collision risk

For implementation work, verify that:

- the Issue is not already owned by another active Agent unless collaboration is explicit;
- there is no conflicting PR implementing the same task;
- the intended files/domain do not materially overlap another active task without coordination;
- prerequisites are satisfied.

Use the resolved Domain scope from Bootstrap as an ownership aid; never treat the Domain identifier itself as a filename pattern, directory prefix, or sufficient proof of ownership.

For optional-Domain Roles, treat `DOMAIN` as the primary specialist focus. Inspect adjacent domains when needed to perform the Role correctly, but do not silently take ownership of another Agent/domain's work.

If collision risk is material, stop mutation and hand off to Integration when it concerns repository integration/dependencies, or request a human decision. If an external Control Plane owns runtime coordination, follow its durable coordination state instead of creating a separate ChatGPT Supervisor Role.

## 6. Determine readiness

Classify the start state as one of:

- `READY`
- `BLOCKED_DEPENDENCY`
- `BLOCKED_ROLE_BOUNDARY`
- `BLOCKED_CONFLICT`
- `STALE_HANDOFF`
- `NEEDS_HUMAN_DECISION`

Only `READY` permits normal execution. `STALE_HANDOFF` requires reevaluation from current GitHub state; it does not automatically mean the underlying work is blocked.

## 7. Startup report

Keep the startup report concise. Include Domain when declared or required.

Example:

```text
ROLE: implementation
DOMAIN: device
ISSUE: #123
STATE: READY
RELATED_PR: none
DEPENDENCIES: satisfied
COLLISION_CHECK: clear
NEXT_ACTION: implement acceptance criteria AC-01..AC-04
```

For an optional-Domain Role operating cross-domain, Domain may be omitted:

```text
ROLE: review
ISSUE: #130
PR: #135
STATE: READY
HANDOFF_HEAD_MATCH: yes
NEXT_ACTION: independently review current PR against durable requirements/evidence
```

## 8. Execute within Role

After Bootstrap, perform only actions allowed by the active Role Contract. If the task evolves beyond that boundary, persist the material state and use the Handoff Protocol rather than silently expanding the Role.

A Domain change must be explicit. It does not change the active Role or grant additional permissions.
