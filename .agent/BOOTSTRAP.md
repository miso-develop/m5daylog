# Agent Bootstrap Protocol

Every agent chat must perform this bootstrap before substantive repository work.

## 1. Identify execution context

Confirm:

- `ACTIVE_ROLE`
- role contract path
- project/repository
- assigned Issue(s), if any
- domain scope for Implementation Agents

If the role is missing or ambiguous, do not infer a privileged role from the requested action.

## 2. Load policy

Read:

1. `.agent/AGENT_CATALOG.md`
2. `.agent/roles/<ACTIVE_ROLE>.md`
3. `.agent/HANDOFF_PROTOCOL.md`
4. relevant Map / Decision / Spec / Task records

For long-running chats, re-read the role contract when the repository indicates that it changed.

## 3. Inspect current repository state

Before repository-changing work, inspect the relevant current state:

- assigned Issue and recent comments
- related PRs
- active branches or ownership markers when available
- prerequisite Issues / PRs
- CI/check status when relevant
- recent changes that may invalidate the task context

Do not rely on stale chat summaries when GitHub contains newer state.

## 4. Check ownership and collision risk

For implementation work, verify that:

- the Issue is not already owned by another active agent unless collaboration is explicit;
- there is no conflicting PR implementing the same task;
- the intended files/domain do not materially overlap another active task without coordination;
- prerequisites are satisfied.

If collision risk is material, stop mutation and hand off to Integration when it concerns repository integration/dependencies, or request a human decision. If an external Control Plane owns runtime coordination, follow its durable coordination state instead of creating a separate ChatGPT Supervisor role.

## 5. Determine readiness

Classify the start state as one of:

- `READY`
- `BLOCKED_DEPENDENCY`
- `BLOCKED_ROLE_BOUNDARY`
- `BLOCKED_CONFLICT`
- `NEEDS_HUMAN_DECISION`

Only `READY` permits normal execution.

## 6. Startup report

Keep the startup report concise. It should contain enough information to establish state, for example:

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

## 7. Execute within role

After bootstrap, perform only actions allowed by the active role contract. If the task evolves beyond that boundary, use the Handoff Protocol rather than silently expanding the role.
