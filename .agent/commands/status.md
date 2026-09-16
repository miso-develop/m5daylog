# /status

command_id: status
alias: /s
version: 2

## Purpose

Refresh authoritative state relevant to the current assignment and report the current work status without advancing the work.

`/status` must not merely repeat remembered chat state. Re-fetch current durable state when repository or coordination state can affect the answer.

## Behavior

1. Preserve the current `ACTIVE_ROLE` and `DOMAIN`.
2. Refresh the latest authoritative state relevant to the active assignment, such as Issue / PR / branch / checks / comments / dependencies / ownership / handoff state as applicable.
3. Reevaluate the current readiness, blocker, finding, or disposition against that refreshed state.
4. Report the current Issue / PR / branch / checks / blocker state as applicable.
5. Identify the next expected action and owning Role.
6. When useful, state what changed from the previously known state.
7. Do not perform implementation, merge, review remediation, or other repository-changing work merely because refreshed state now permits work.

## Output

Keep the report concise and include, when relevant:

- current assignment;
- current state;
- related PR;
- checks / evidence state;
- blockers;
- meaningful changes since the previous known state;
- next action;
- next owning Role.

## Alias

`/s` is an exact alias of `/status` and follows this same contract.
