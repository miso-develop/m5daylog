---
name: codebase-design
description: Module boundary / interface / seam / ownership を設計する。
---

# Codebase Design

- Change理由が異なる責務を分離する。
- Cross-boundary behavior は explicit contract を持つ。
- Hidden global state / bidirectional dependency / duplicated protocol knowledge を避ける。
- Test seam を production security control の bypass として設計しない。
- M5Daylog の Device/PC shared contract は `contracts/` と Integration ownership を優先する。
- Minimal abstraction から開始し、Task requirement で正当化できない generalization を追加しない。
