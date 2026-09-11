# Agent Instructions

## Highest-priority security rule

Account、token、secret、credential、private key、session material の漏洩防止は、利便性、debug速度、feature velocity、test convenience、実装上の近道より常に優先する。

Credential-like value を読む・書く・表示する・logする・uploadする・commitする・外部へ送る前に `SECURITY.md` を参照する。実値が必要に見える場合でも、synthetic/public test data で再設計する。Security rule を一時的に弱めて作業を進めてはならない。

Raw audio、transcript、daily log、speaker metadata は認証情報とは別の **private user data** であり、同様に public GitHub / log / artifact / fixture / screenshot / AI prompt / external request へ出さない。

## Source of truth

優先順位は次の通り。

1. ユーザーの最新かつ明示的な指示
2. `SECURITY.md` の security invariant
3. `PROJECT.md` の project-wide invariant
4. GitHub `[Map]` / `[Decision]` / `[Spec]` / `[Task]` と Pull Request
5. repository の code / configuration / durable docs / tests

既存 code/test は重要な evidence だが、明示 requirement と矛盾する場合に requirement を黙って変更しない。

## Work item eligibility

Production code または repository current truth を変更する implementation iteration は、原則として GitHub に実在する open な `[Task]` を1件だけ選択する。

Task は以下を満たす必要がある。

- Parent / Related Spec が追跡可能である。
- blocker がすべて merge / close 済みである。Green-but-unmerged PR は blocker 解消ではない。
- Acceptance / completion criteria が外部から判定可能である。
- 同一 Task の unfinished PR / branch がある場合は新規 branch を作らず resume する。

Task が存在しない場合は production change を推測して開始しない。必要に応じて `wayfinder` -> `to-spec` -> `to-tickets` で具体化する。

## One implementation iteration

1. `PROJECT.md`、`SECURITY.md`、対象 Task / parent Spec / linked Decisions、関連 code/tests を読む。
2. `agent/PARALLEL-WORK.md` の mandatory preflight を実行し、Task ownership と change surface を確認する。
3. unfinished branch/PR がなければ observed latest `main` から canonical `task/<issue-number>` branch を作り ownership claim する。
4. first meaningful write 前に GitHub coordination state を再確認する。
5. Task と既存挙動維持に必要な最小範囲だけ変更する。Scope expansion 前には parallel preflight を再実行する。
6. Secret handling、private-data boundary、logging、artifact、network behavior への影響を明示的に review する。
7. 外部観測可能な behavior を優先して tests/checks を追加・更新し、repository-defined required checks を実行する。
8. `code-review` で requirement / security / engineering / parallel merge safety を確認する。
9. Commit / pushし、PR に Parent Spec と `Closes #<task>` を記載する。
10. Merge 直前に latest `main`、open/draft PR、branch HEAD、semantic overlap を再確認し、必要なら branch update + affected checks rerun を行う。
11. PR merge により Task が close されて初めて iteration 完了とする。同じ iteration で次 Task に進まない。

## Security-sensitive behavior

- Real token / password / API key / PAT / OAuth credential / cookie / private key / recovery material を要求しない、表示しない、保存しない。
- `.env`、credential store、process environment 全体を debug目的で dump しない。
- Secret-bearing value を command line argument、URL/query、exception、trace、telemetry、serial output、console log へ載せない。
- Local audio / transcript content を bug report、test fixture、PR evidence として使用しない。
- External upload / analytics / remote error reporting を private-data path に追加する変更は security-sensitive とし、明示 Spec / review なしで追加しない。
- `.env.example` は value-free に保つ。
- Secret exposure の疑いがある場合は作業を止め、`SECURITY.md` の incident response に従う。

## Incomplete work

完了できない場合は新しい scope を増やさず `handoff` を使用する。Checkpoint は Issue / PR に残し、branch/HEAD、completed、verification、reservation、blocker、next action を記録する。Secret / private user data は記録しない。

## Engineering constraints

- 要件にない dependency、abstraction、大規模 refactor を追加しない。
- Test / Acceptance Criteria / security control を「通すため」に弱めない。
- Temporary debug code、generated dump、credential、private user data を commit しない。
- `.cmd` を追加する場合は CRLF を維持し、新規 Windows command entrypoint は `.cmd` とする。

## Supporting rules

- Work item lifecycle: `agent/WORK-TRACKING.md`
- Parallel implementation: `agent/PARALLEL-WORK.md`
- Security: `SECURITY.md`
- Reusable procedures: `.agents/skills/`

## Done

Task を完了扱いにできるのは、Acceptance Criteria、repository-defined checks、security review、code review、parallel exact-head safety がすべて満たされ、PR が merge され対象 Task が close された場合だけとする。
