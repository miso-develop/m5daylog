# Work Tracking

## Canonical work item types

| Prefix | Purpose |
| --- | --- |
| `[Map]` | Large/ambiguous work の decision map |
| `[Decision]` | Map 内の1つの question / investigation |
| `[Spec]` | 実装前に確定した externally meaningful contract |
| `[Task]` | Production/current-truth を変更する vertical implementation ticket |

GitHub Issues / Pull Requests が planning / decision / implementation state の durable coordination source である。Repository 内に別の進捗台帳を作らない。

## Map

Map は outcome、scope、unknowns、decision questions、exit condition を記述する。Map 自体を implementation ticket として使わない。

## Decision

Decision は question、options/evidence、decision、consequences、follow-up を記録する。Settled knowledge が current system truth として必要なら Spec / durable repository docs / code に昇格する。

## Spec

Spec は implementation-independent な contract を記述する。最低限 purpose、scope/out-of-scope、observable behavior、interfaces/data boundaries、error behavior、security/privacy constraints、acceptance criteria を含める。

## Task

Task は1つの vertical outcome を持ち、Parent/Related Spec、Agent、Priority、Blocked by / Depends on、Scope、Completion criteria、Evidence を明示する。

Task は「ファイルを変更する」ではなく externally judgeable な結果を表す。Blocker は Issue close / prerequisite merge で解消される。Green-but-unmerged PR は blocker 解消ではない。

## Branch / PR ownership

- Canonical implementation branch: `task/<issue-number>`
- 同じ Task の branch / PR は1本だけ。
- Remote branch の存在は Task ownership claim の一部である。
- Alternative branch 名で既存 claim を迂回しない。
- PR body は Parent Spec と `Closes #<task>` を含める。

## Repository knowledge lifecycle

Issue/PR discussion にある知識のうち、将来も current behavior を理解・変更するため必要なものは、Task 完了までに適切な durable source へ昇格する。

- runtime truth -> code/config
- interface/schema -> contract/code + tests
- operational invariant -> PROJECT/SECURITY/agent docs
- user/developer operation -> README/docs

Closed Issue を唯一の current specification にしない。

## Handoff

未完了 work を別 session / agent へ移す時だけ current Task Issue または PR に checkpoint を残す。

Minimum fields:

- Branch / HEAD
- PR
- Completed
- Verification
- Repository knowledge pending promotion
- Reservation / planned change surface
- Blocker
- Next action
- References

Credential、token、secret、private user data を handoff に含めない。Handoff は branch claim を解放しない。

## Completion

Task は linked PR が merge され、Acceptance Criteria と required checks が満たされ、Issue が close された時のみ complete とする。
