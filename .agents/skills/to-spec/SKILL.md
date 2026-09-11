---
name: to-spec
description: Settled Map/Decision から externally meaningful `[Spec]` Issue を作る。
---

# To Spec

Spec は implementation plan ではなく contract とする。

最低限:

- Purpose / user-visible outcome
- In scope / out of scope
- Observable behavior
- Interfaces / data ownership / state transitions
- Failure / retry / idempotency behavior
- Security / privacy constraints
- Compatibility / migration constraints
- Acceptance criteria
- Related Decisions / parent Map

Unknown decision を Spec 内で暗黙に決めない。Security-sensitive behavior は `SECURITY.md` を参照し、より弱い contract を書かない。
