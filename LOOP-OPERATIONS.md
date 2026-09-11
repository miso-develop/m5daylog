# Loop Engineering Operations

## 1. 新しい大きな要件

曖昧さ・複数の設計判断を含む場合は `[Map]` を作り、question を `[Decision]` に分離する。Settled decisions から `[Spec]` を作成し、実装可能な vertical slice を `[Task]` に分解する。

```text
request -> [Map] -> [Decision]* -> [Spec] -> [Task]* -> PRs
```

小さく明確な変更は Map / Decision を省略して Spec / Task へ進めてよい。ただし production implementation は Task を持つ。

## 2. 実装開始

- Ready Task を1件選ぶ。
- latest `main`、open/draft PR、non-main branches、open Tasks を確認する。
- `agent/PARALLEL-WORK.md` の parallel eligibility gate を通す。
- Canonical branch は `task/<issue-number>`。
- Branch claim 後、first write 前に coordination state を再確認する。

## 3. 実装中

- Task scope を超えない。
- Shared file / subsystem / protocol / schema / dependency / workflow / security boundary に scope が広がる前に再 preflight する。
- Security-sensitive path では `SECURITY.md` を先に読む。
- Real secret / private user data は test/evidence に使わない。

## 4. PR

PR には最低限以下を記載する。

- Parent Spec
- `Closes #<task>`
- Scope / out-of-scope
- Tests / checks 実行結果
- Security impact
- Parallel reservation / merge-safety result

Meaningful implementation ができた時点で早めに Draft PR を作成して ownership を可視化してよい。

## 5. Merge

Merge 直前に exact PR head と latest `main` を再確認する。Git text merge が可能でも semantic contract overlap があれば安全とはみなさない。Main が materially advanced した場合は branch を更新して affected checks を再実行する。

## 6. 中断 / 再開

中断時のみ handoff comment を Issue / PR に残す。Handoff は source of truth の代替ではなく resume pointer である。Resume 時は Issue/PR、Spec、branch HEAD、latest main、parallel reservations を読み直す。

## 7. Status確認

`loop-status` は GitHub Issues / PRs / branches を read-only で集約し、ready / blocked / in-flight / handoff を判断する。Chat memory を progress ledger として使わない。

## 8. Security incident

Secret / token / credential exposure の疑いが生じた場合、通常実装を停止する。値を再掲せず、まず authoritative source で revoke / rotate し、その後 repository/history/log/artifact surface を調査する。詳細は `SECURITY.md`。
