---
name: tdd
description: Observable behavior を red -> green -> refactor の短い loop で実装する。
---

# TDD

1. Task Acceptance Criteria を externally observable behavior に変換する。
2. 失敗する最小 test を追加し、期待理由で red になることを確認する。
3. 最小 implementation で green にする。
4. Behavior を変えず必要な refactor を行う。
5. Regression / edge / failure path を追加する。

Real credential、raw audio、personal transcript を fixture に使わない。Synthetic/public data だけで再現する。
