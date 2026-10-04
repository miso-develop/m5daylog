# Human Gate Public-Text Publication Rules

version: 1

## 1. Purpose

These rules define the public-text boundary for generated Human Gate instructions, durable Human Gate evidence, and other Human Gate text published by repository-controlled tooling.

They supplement `README.md`, `EXECUTION_RULES.md`, and `COMMAND_RULES.md`. They do not change product Acceptance Criteria or replace independent Review / Security responsibilities.

## 2. Three distinct controls

Keep these controls separate:

1. **Policy requirement** — public Human Gate text must contain only public-safe information and must follow the confidentiality rules in `README.md`.
2. **Controlled pre-publication validation** — when repository/runtime tooling controls publication, the serialized candidate text must pass `scripts/security_scan.py` public-text validation before the publication API/action is invoked.
3. **Reactive GitHub-surface scanning** — a separate repository control may scan Issue, PR, review, or comment content after GitHub has accepted it. Reactive scanning is defense in depth; it did not prevent the original publication.

Repository policy and Actions cannot hard-block arbitrary text a human enters directly through the native GitHub UI. Native-UI publication therefore remains subject to the policy requirement and any reactive detection available after publication.

## 3. Controlled publication contract

A controlled publisher must use this sequence:

```text
candidate public instruction/evidence
 -> serialize complete public text
 -> public-text validation
 -> PASS only
 -> publication
```

A finding or validator error aborts the publication attempt. Do not publish first and sanitize afterward.

Do not pass candidate text as a command-line argument. Supply it through stdin or an explicitly supplied local file:

```text
python3 scripts/security_scan.py --public-text-stdin --label human-gate/instruction
python3 scripts/security_scan.py --public-text-file <candidate-file> --label human-gate/evidence
```

The label is public diagnostic metadata, not candidate content. Use a short abstract label composed only of public-safe characters. Local file paths used to feed the validator are transient operator/runtime data and must not be copied into public evidence.

Validator exit status is part of the publication gate:

- `0`: validation passed; publication may continue if all other requirements are satisfied;
- `1`: one or more findings; publication is prohibited;
- `2`: input/scanner validation could not be completed; publication is prohibited.

Finding/error output may contain only the safe input label, line number where available, rule identifier, generic message, and counts. It must not contain candidate text, matched values, source excerpts, captures, or the local candidate-file path.

## 4. Generated Human Gate instructions

Programmatically generated instructions must use neutral placeholders for machine-local values before validation, including `<repo-root>`, `<worktree>`, `<evidence-dir>`, `<serial-port>`, `<device-root>`, and `<revision>`.

The generator must validate the final serialized instruction, not an earlier template fragment. A successful template scan does not authorize publication of later interpolated text.

If the generator cannot execute the validator or cannot establish its result, treat the publication attempt as blocked rather than bypassing validation.

## 5. Durable Human Gate evidence

Prefer structured, allowlisted evidence fields over redaction of arbitrary raw terminal output.

Public durable evidence should contain only the minimum acceptance data required for the Human Gate, such as:

- public Issue / PR identity;
- exact public revision identity;
- Acceptance Criterion / matrix identity;
- safe command result metadata such as exit status and generic result class;
- sanitized physical/manual observation required to evaluate the criterion;
- final `PASS` / `FAIL` / `BLOCKED` / `NOT_RUN` disposition.

Richer command transcripts, machine-local paths, host/device identifiers, raw payloads, and diagnostic logs remain local unless a separate process reduces them to public-safe fields.

After evidence fields are serialized into the exact text intended for publication, validate that serialized text through the same public-text validator. A structured source object does not bypass final serialization validation.

## 6. Evidence and validator failures

A publication-validator failure is a publication/harness failure, not automatically a product failure or Human Gate acceptance failure.

Preserve valid local product observations when they remain independently usable. Correct the public serialization or validator/harness defect, then revalidate the candidate. Do not repeat a physical or destructive acceptance action merely because public-text post-processing failed when the underlying observation remains valid.

Never copy a rejected matched value into an Issue, PR, comment, remediation note, or error report while explaining the validation failure.
