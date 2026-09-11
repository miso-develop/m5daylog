---
name: implement
description: Ready `[Task]` を安全に branch / tests / review / PR / merge まで進める。
---

# Implement

1. Task / Parent Spec / related Decisions / PROJECT / SECURITY を読む。
2. `agent/PARALLEL-WORK.md` preflight を行う。
3. Existing branch/PR がなければ latest main から `task/<issue>` を atomic claim する。
4. Task scope の最小 change plan を作り、security/private-data impact を確認する。
5. Behavior change は可能な限り test-first で実装する。
6. Scope expansion 前に parallel preflight をやり直す。
7. Repository-defined tests/static checks/security scan を実行する。未導入 check を成功扱いしない。
8. `code-review` と必要な `security-review` を実施する。
9. Commit/pushし、Parent Spec + `Closes #<task>` を含む PR を作成する。
10. Exact-head merge preflight 後に merge する。
11. Issue close を確認して終了する。同 iteration で次 Task を開始しない。

External Verifier repository/service 固有の contract は前提にしない。
