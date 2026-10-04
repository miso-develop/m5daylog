# Human Gate Policy

version: 2

## Purpose

This directory defines repository-wide operating rules for physical, device, host-integration, manual-observation, and other Human Gate verification.

Human Gate evidence is part of acceptance. It must be reproducible, attributable to an exact repository revision, and kept distinct from automated test evidence.

## Required reading

When a task involves planning, executing, interpreting, recording, or resuming a Human Gate, read these files before giving execution instructions:

1. `.agent/HUMAN_GATE/EXECUTION_RULES.md`
2. `.agent/HUMAN_GATE/COMMAND_RULES.md`
3. `.agent/HUMAN_GATE/COMMAND_FAILURE_PLAYBOOK.md`

`EXECUTION_RULES.md` defines acceptance-session semantics, baseline, cycle validity, evidence classification, and final dispositions.

`COMMAND_RULES.md` defines the command packet, PowerShell/native-process, timeout, toolchain, reset/session-boundary, and observation requirements that Human Gate operator commands must follow.

`COMMAND_FAILURE_PLAYBOOK.md` records reusable command/harness failure modes, their diagnostic signatures, prevention rules, and safe recovery methods. When a new command-side defect causes or nearly causes an invalid Human Gate verdict, add a generalized public-safe entry there rather than leaving the lesson only in chat history.

These rules apply in addition to the active Role Contract, approved specification / Acceptance Criteria, current Issue / PR state, and `.agent/HANDOFF_PROTOCOL.md`.

If a Human Gate rule conflicts with an approved product requirement or Role boundary, do not silently change the requirement. Escalate through the normal Specification / Implementation / Review / Integration / Human workflow as appropriate.

## Confidentiality and repository hygiene

The repository may be public. Human Gate documentation committed to the repository MUST NOT contain machine-specific or user-specific environment information unless the human owner explicitly approves disclosure.

Never persist examples or evidence containing any of the following unless explicitly approved:

- absolute local filesystem paths or drive letters;
- local usernames, home directories, workstation names, or network share names;
- private repository names or private repository URLs;
- credentials, tokens, secrets, environment-variable values, or authentication material;
- device serial numbers, host identifiers, or other identifiers that are not necessary for public acceptance evidence;
- raw personal content captured by the product under test.

Use neutral placeholders such as `<repo-root>`, `<worktree>`, `<evidence-dir>`, `<serial-port>`, `<device-root>`, and `<revision>` in committed documentation.

A local Human Gate session may use machine-specific values transiently in the operator's shell, but those values must be sanitized before any result is persisted to public durable state.

## Evidence boundary

Keep these categories separate:

- automated test evidence;
- command/tooling execution evidence;
- physical/manual Human Gate observations;
- acceptance verdicts.

A successful command does not prove a physical acceptance criterion. A command-wrapper failure does not by itself prove a product failure. Record the observed product behavior independently from the harness/tooling behavior.

When a parser or wrapper verdict is later shown to be wrong, preserve the underlying raw observation, correct the harness verdict, and follow `EXECUTION_RULES.md` for whether the physical action must be repeated. Do not preserve a known false harness classification as product evidence.
