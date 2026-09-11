---
name: to-tickets
description: `[Spec]` を independently testable な vertical `[Task]` Issues に分解する。
---

# To Tickets

- 1 Task = 1 externally judgeable vertical outcome。
- Component/file 単位だけの horizontal ticket を乱造しない。
- Parent Spec、Agent、Priority、Blocked by/Depends on、Scope、Completion criteria、Evidence を書く。
- Boundary contract の ownership は明示する。
- 並行実装候補は file だけでなく subsystem / protocol / schema / security boundary の overlap で評価する。
- Security control を「後で入れる」前提の unsafe intermediate Task を作らない。
