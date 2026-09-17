# /bootstrap

command_id: bootstrap
alias: /b
version: 3

## Purpose

Activate an Agent chat from explicit arguments, or re-run Bootstrap for the current activation when no Role is supplied.

## Syntax

```text
/bootstrap [<role>] [options]
/b [<role>] [options]
```

Role may be the canonical Role id or its `short_name` from `.agent/roles.yaml`.

Activation options:

```text
-d, --domain <domain>
-i, --issue <number|#number|none>
-p, --objective <text>
-g, --generation <positive-integer>
--no-start
```

`--no-start` is long-only. Unknown options, invalid values, missing option values, or invalid Role/Domain combinations are usage errors. Return a concise CLI-style usage error and do not guess missing values.

## Activation mode

When `<role>` is supplied:

1. Treat the command as an explicit human Role activation; changing the current Role or Domain is allowed because it is explicit, not implicit.
2. Resolve canonical Role and `short_name` from `.agent/roles.yaml`.
3. Apply that Role's Domain mode. Required Domain must be supplied; forbidden Domain must not be supplied. Optional Domain may be omitted.
4. Normalize Domain to lowercase and accept project-specific identifiers matching `[a-z0-9][a-z0-9._-]*`. Do not infer Domain scope from its spelling.
5. Normalize Issue to `#<number>` or `none`. If Issue is omitted, Implementation uses `none`; other Roles leave Issue unset.
6. Normalize Objective to one line when supplied; otherwise leave it unset.
7. Use Generation `1` when omitted. Generation must be a positive integer.
8. Replace the current activation state with the resolved `ACTIVE_ROLE`, optional `DOMAIN`, optional `ASSIGNED_ISSUE`, optional `OBJECTIVE`, and `CHAT_GENERATION`.
9. Run `.agent/BOOTSTRAP.md` for that activation.

## Re-bootstrap mode

When no `<role>` is supplied, preserve the current activation state and re-run `.agent/BOOTSTRAP.md`. If an existing chat has no `CHAT_GENERATION`, treat it as generation `1` for reporting. Activation options other than `--no-start` require an explicit Role and are usage errors without one.

## Output and execution

After Bootstrap, emit:

```text
# Chat Opening — <Role Display Name>[-<domain>]#<generation>
```

followed by the normal concise startup report.

Unless `--no-start` is present, automatically continue into substantive Role work only when Bootstrap returns `READY` and the current Issue / Objective / durable state identifies an actionable target. If there is no actionable target, stop after the startup report.

`--no-start` always stops after Bootstrap/startup reporting. Any non-`READY` state also stops automatic execution.

## Safety

- Pseudo commands do not expand Role permissions.
- Do not infer a missing required Domain, invalid option, Issue, Objective, or Role.
- Do not weaken Bootstrap, ownership, collision, visibility, Security, Review, Integration, or Human Gate requirements to enable automatic start.
- `/b` is an exact alias of `/bootstrap` and follows this same contract.
