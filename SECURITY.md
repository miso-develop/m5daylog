# Security Policy

## Highest-priority invariant

**Account / credential / token / secret leakage prevention is the highest-priority invariant of this repository.**

Security overrides convenience, debugging speed, feature velocity, test convenience, and implementation shortcuts.

This repository is public. Anything committed to Git, pasted into an Issue/PR/comment, emitted to Actions/logs, attached, screenshotted, included in an artifact, or sent to an external service must be assumed disclosed outside the local machine.

If sensitivity is uncertain, treat the value/data as sensitive and fail closed.

## 1. Never expose real authentication material

The following must never appear in repository content, Git history, Issues, Pull Requests, review comments, logs, artifacts, screenshots, fixtures, examples, documentation payloads, AI prompts, chat attachments, or external requests:

- passwords / passphrases / recovery codes
- GitHub PATs, fine-grained PATs, GitHub App private keys, installation/user tokens
- OAuth access / refresh tokens
- API keys and service tokens, including Hugging Face / model-registry credentials
- session cookies, bearer tokens, authorization headers, signed URLs containing credentials
- cloud credentials, service-account credentials, webhook secrets
- SSH/TLS/code-signing/private keys, seed material
- Wi-Fi credentials or credential-bearing configuration exports
- database / service URIs containing username/password or token material
- OS credential-store exports or process/environment dumps that may contain credentials
- any value that enables authentication, impersonation, privilege escalation, decryption, or secret recovery

Encoding does not make a secret safe. Base64, hex, URL encoding, QR/image representation, archive/compression, encryption under an exposed/reusable key, or partial masking does not authorize publication.

**Never ask a user to paste a real token/secret into an Issue, PR, chat, source file, test, or command transcript.** Ask them to configure it through the documented secure local/CI mechanism instead.

## 2. Private user data is a separate protected class

M5Daylog handles data that may capture highly private conversations. The following are private user data even when they are not authentication material:

- raw / processed audio recordings
- speech-to-text transcript text and timestamped segments
- speaker embeddings, voiceprints, diarization labels linked to real people
- daily logs / summaries derived from real recordings
- local file metadata that reveals private recording names, locations, people, or schedules
- diagnostic dumps containing any of the above

Real private user data must not be used as Git fixtures, Issue/PR evidence, screenshots, CI artifacts, example output, logs, or external debugging payloads.

Synthetic fixtures must remain safe if decoded or printed in full. Public test vectors/datasets may be used only when their license and public nature are clear and the Task requires them.

## 3. Local-first network boundary

PoC audio processing is local-first. Raw audio, transcripts, daily logs, speaker data, or derived private content must not be uploaded to third-party APIs, analytics, telemetry, remote error reporting, or model services by default.

A change that sends protected data off the local machine requires an explicit `[Decision]` / `[Spec]`, documented destination/data classes/retention/failure behavior, and security/privacy review before implementation.

Downloading model binaries/metadata is distinct from uploading user data. Model acquisition may require a short-lived external token, but the token must be supplied through a secure local mechanism and never logged or persisted into repository content.

## 4. Safe credential input

### Local development

- `.env.example` is tracked and **value-free**: every non-comment entry is `KEY=`.
- Actual `.env` / `.env.*` values are local-only and Git-ignored.
- `.env` is plaintext convenience storage, **not a secure secret store**.
- Prefer OS credential stores or dedicated secret managers for persistent credentials.
- Do not put secret values in command-line arguments, because process listings/shell history may expose them.
- Do not put secrets in URL/query/fragment values.
- Do not dump the complete environment (`env`, `set`, `Get-ChildItem Env:*`, equivalent) for debugging.
- Do not read arbitrary credential files merely to confirm configuration; prefer existence/variable-name/status checks that do not reveal values.

### CI / GitHub Actions

- Use GitHub secret/environment facilities for secrets; never repository variables/files containing real secret values.
- Workflow permissions must be least privilege.
- Third-party Actions must be pinned to an immutable full commit SHA when practical.
- Checkout must not persist credentials when later steps do not need them.
- Never echo or serialize secret contexts.
- Do not upload artifacts that may contain `.env`, credentials, private user data, dumps, caches with tokens, or authenticated config.

## 5. Logging / diagnostics

Never log or include in exceptions/traces/telemetry:

- token/key/password/credential values
- authorization/cookie headers
- credential-bearing URLs
- raw audio bytes or encoded audio payloads
- transcript text from real recordings
- real daily-log content
- speaker embeddings/voiceprints
- environment/credential-store dumps

Logs should use allowlisted metadata such as stage, duration, counts, redacted error class, synthetic IDs, and status. Error messages identify the failure class without reproducing the sensitive input.

Security and privacy redaction rules apply equally to debug builds and local troubleshooting transcripts intended for GitHub/chat.

## 6. Tests / fixtures

Allowed:

- clearly synthetic credentials generated only for tests
- public standard test vectors
- synthetic text/speaker IDs
- deliberately generated synthetic or licensed-public audio fixtures with explicit provenance

Forbidden:

- personal recordings
- real meeting/conversation audio
- real transcripts/day logs
- credentials copied from local configuration
- exported browser/OS/session/auth state
- user-specific dumps

The repository security scanner allowlist may only exempt exact hashed files under designated public/synthetic fixture directories. An allowlist entry is reviewable evidence, not permission to store real secret/private data.

## 7. Repository hygiene

- Review staged/tracked changes before every commit.
- `.gitignore` and scanner checks are defense in depth; they are not authorization to keep secrets inside the repository worktree.
- Generated recordings/transcripts/day logs/dumps/captures stay in ignored local-only paths.
- Never commit private-key/container credential formats unless an explicit public/synthetic fixture exception is justified and reviewed.
- Do not use `git remote -v` or similar output as evidence if a remote URL may contain credentials; redact the entire credential-bearing component.
- Do not paste full HTTP request/response headers from authenticated traffic.

## 8. Security scanner policy

`scripts/security_scan.py` is a repository-owned, dependency-free leakage guard. It scans Git-tracked files and fails closed when it cannot enumerate/read them.

It checks high-confidence credential signatures, private-key markers, credential-bearing literal assignments, value-bearing `.env` templates, dangerous logging patterns, credential/key/dump paths, and tracked audio files unless an exact synthetic/public fixture hash is allowlisted.

The scanner must never print the matched secret value. Findings contain only path, line, rule, and generic message.

`.security-scan-allowlist.json` is expected to remain empty in normal development. Exceptions require exact file SHA-256, designated fixture location, rule, kind, and reason. Stale exceptions fail the scan.

Security scan failure is a blocking defect. Do not weaken regex/rules, broaden allowlists, or delete tests merely to make CI pass.

## 9. Security-sensitive changes

Explicit security/privacy review is required for changes affecting:

- account/token/secret acquisition, storage, lifetime, rotation, or transport
- `.env` / local configuration loaders
- logging, diagnostics, crash/core dumps
- audio/transcript/day-log storage or retention
- speaker embeddings / diarization identity data
- network upload, analytics, telemetry, remote error reporting
- model registry/download authentication
- IPC / USB / serial contracts carrying protected data
- CI permissions, Actions, artifacts, caches, release/signing pipeline
- third-party dependencies in protected-data paths
- encryption/key management if introduced later

Security regressions are blocking even when functional tests pass.

## 10. Incident response

If a real secret/token/credential may have been exposed:

1. **Do not reproduce the value while investigating.**
2. Assume compromise immediately.
3. Revoke / rotate / replace the credential at its authoritative source **before relying on repository cleanup**.
4. Remove the exposed material from current visible surfaces where practical.
5. Inspect Git history, branches, PRs/Issues/comments, Actions logs, artifacts, caches, release assets, attachments, mirrors, and copied troubleshooting transcripts for further exposure.
6. Rewrite unmerged history when useful, but do not claim remote copies are erased.
7. Determine the trust-boundary failure and add a preventive test/rule.

Deleting a file/commit/comment is not sufficient remediation. Credential rotation is mandatory when a real credential was exposed or exposure cannot be ruled out.

If private user audio/transcript/day-log data is exposed, stop propagation, remove accessible copies where practical, identify every destination/artifact/cache, and review the workflow that allowed the data to leave its intended local boundary. Do not repost the content while documenting the incident.

## 11. Threat-model boundary

This policy strongly targets accidental publication, source/history leakage, CI/log/artifact leakage, unsafe debugging, unintended external upload, and credential misuse during development.

It does not by itself guarantee protection against a fully compromised developer OS, malicious privileged process, physical memory extraction, compromised dependency, or account takeover at the authoritative service. Those risks require controls outside this repository as well.
