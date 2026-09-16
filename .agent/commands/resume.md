# /resume

command_id: resume
alias: /r
version: 3

## Purpose

Resume work for the current Agent from the latest durable project state.

Unlike `/continue`, which recovers the immediately preceding interrupted ChatGPT task, `/resume` re-establishes the Agent's current actionable work by inspecting the repository and other authoritative coordination state.

Use this command when the user wants the Agent to check the latest Issue / PR state and continue whatever work currently belongs to this Role and Domain.

## Scope

`/resume` is repository- and workflow-oriented.

It may inspect relevant:

- assigned Issues and recent comments;
- related or assigned PRs;
- durable handoffs;
- branches and commits;
- CI / check results;
- dependency state;
- ownership / assignment markers;
- external Control Plane coordination state when the project uses one;
- approved Map / Decision / Spec / Task records needed to determine readiness.

## Source-of-truth principle

Always refresh authoritative durable state needed to make the resume decision. Do not merely rely on remembered chat state when repository or coordination state may have changed.

Reconstruct the Agent's current work from authoritative project state before deciding what to execute.

When an external Control Plane owns assignment, ownership, scheduling, or runtime coordination, respect that durable state rather than inventing a competing assignment.

## Work selection priority

Prefer actionable work in this order:

1. explicitly assigned unfinished work for the current chat/Agent;
2. a durable handoff addressed to the current Role and, when applicable, Domain;
3. work already owned by this Agent or current Role/Domain;
4. work explicitly marked ready for the current Role and Domain under the project's coordination rules.

Do not claim unrelated unassigned work merely because it appears executable.
Do not take work owned by another active Agent unless collaboration or reassignment is explicit.

## Behavior

1. Preserve the current `ACTIVE_ROLE` and `DOMAIN`.
2. Refresh and inspect the latest relevant durable project state.
3. Identify the highest-priority work item that currently belongs to this Agent and is actionable within the active Role Contract.
4. Verify dependencies, ownership, collision risk, required reviews, CI/checks, and Human Gates as applicable.
5. If executable work exists, proceed with it rather than stopping after a status summary.
6. Continue until one of the following is true:
   - the selected work is complete;
   - no further work currently belongs to this Agent;
   - a genuine blocker is reached;
   - a Role boundary requires handoff.

## Safety

- Do not silently change `ACTIVE_ROLE` or `DOMAIN`.
- Do not bypass Role boundaries, Human Gates, required decisions, dependencies, or ownership rules.
- Do not duplicate repository mutations that durable state shows are already complete.
- Do not infer completion from an old chat message when current repository state disagrees.
- Do not self-assign unrelated backlog work outside the project's assignment/coordination rules.

## Outcomes

Classify the result as one of:

- `RESUMED`: actionable work was identified from current durable state and execution resumed;
- `COMPLETE`: the current assigned/owned work is complete;
- `NO_ACTIONABLE_WORK`: no eligible work currently belongs to this Agent;
- `BLOCKED`: a genuine blocker prevents further work;
- `HANDOFF_REQUIRED`: the next action belongs to another Role.

When no actionable work exists, blocked, or handing off, state the relevant repository state and required next action precisely.

## Alias

`/r` is an exact alias of `/resume` and follows this same contract.
