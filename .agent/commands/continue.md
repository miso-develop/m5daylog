# /continue

command_id: continue
alias: /c
version: 2

## Purpose

Recover and continue the immediately preceding unfinished ChatGPT task.

This command is the shorthand for the user's intent: "if the previous ChatGPT work stopped, continue it."

It is specifically intended for cases where a ChatGPT response, tool execution, or multi-step operation was interrupted, truncated, stalled, forcibly stopped, or otherwise failed to complete visibly.

## Scope

`/continue` is scoped to the current conversation and the task that was already in progress immediately before the command.

It must not switch to a different assignment or search the repository for unrelated new work.

Repository state may still be inspected when necessary to verify whether side effects from the interrupted task actually completed.

## Recovery principle

Do not assume an interrupted operation either succeeded or failed.

Reconstruct the visible task state from the current conversation and verify uncertain side effects against authoritative state before retrying them.

## Behavior

1. Preserve the current `ACTIVE_ROLE`, `DOMAIN`, and active assignment.
2. Identify the immediately preceding task and intended outcome.
3. Determine which visible steps were completed and which remain unfinished or uncertain.
4. Verify uncertain operations before repeating them, especially repository mutations and external side effects.
5. Continue from the next unfinished action when it is permitted by the active Role Contract.
6. Continue until one of the following is true:
   - the current task is complete;
   - a genuine blocker is reached;
   - a Role boundary requires handoff.

## Safety

- Do not duplicate already completed mutations.
- Do not claim to restore hidden or unobservable execution state.
- Do not switch to another Issue, PR, or assignment merely because the current task is already complete.
- Do not bypass Human Gates, Role boundaries, dependencies, ownership constraints, or required decisions.
- Do not stop after merely reporting status when the current task still has executable work.

## Outcomes

Classify the result as one of:

- `CONTINUED`: the interrupted task was safely continued;
- `COMPLETE`: the current task was already complete or became complete during continuation;
- `BLOCKED`: a genuine blocker prevents further work;
- `HANDOFF_REQUIRED`: the next action belongs to another Role.

When blocked or handing off, state the blocker or required next Role/action precisely.

## Alias

`/c` is an exact alias of `/continue` and follows this same contract.
