# M5Daylog

M5Stack device と Windows PC を組み合わせた local-first audio lifelog PoC です。

## Development workflow

本 repository は Loop Engineering の Issue-driven workflow を採用します。

```text
[Map] -> [Decision] -> [Spec] -> [Task] -> branch -> tests/review -> PR -> merge
```

- product / architecture の planning state は GitHub Issues / Pull Requests を正とします。
- repository の current truth は code / configuration / durable documentation / tests に残します。
- production implementation は ready な `[Task]` を1 iterationにつき1件だけ扱います。
- 複数 chat / agent / human worker の並列作業は `agent/PARALLEL-WORK.md` に従います。
- 中断時のみ `handoff` を使い、Issue / PR に再開 checkpoint を残します。
- security handling は `SECURITY.md` を最優先します。

詳細は `AGENTS.md`、`LOOP-OPERATIONS.md`、`agent/WORK-TRACKING.md` を参照してください。

## Repository layout

詳細な責務境界は GitHub Spec #34 を正とします。

- `firmware/`: device firmware
- `pc/`: Windows PC application
- `contracts/`: Device / PC 間 contract
- `fixtures/`: synthetic test data only
- `docs/`: durable architecture / operations documentation
- `scripts/`: repository-owned utility scripts
- `.github/`: Issues / PR / CI configuration

## Verification model

この repository は repository-owned tests / static checks / CI を検証根拠とし、外部 Verifier service / repository 固有 contract を前提にしません。

## Security

Account credential、token、secret、private key、session credential の漏洩防止は最上位 invariant です。Raw audio、transcript、daily log、speaker metadata も private user data として扱います。実データを Issue / PR / log / fixture / artifact / screenshot / AI prompt / external request に貼らないでください。詳細は `SECURITY.md` を参照してください。

## Provenance

Loop Engineering の構成は `miso-develop/loop-boilerplate-private` の current framework（2026-09-10 時点 r78）を consumer repository 向けに整理し、Verifier surface を除外しています。Verifierless consumer / security pattern は `miso-develop/m5authenticator` も参照しています。
