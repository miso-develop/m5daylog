---
name: handoff
description: 未完了 work の resumable checkpoint を current Issue / PR に残す。
---

# Handoff

Completed work には不要。中断時だけ current Task Issue または PR へ以下を残す。

- Branch / HEAD
- PR
- Completed
- Verification
- Repository knowledge pending promotion
- Reservation / planned change surface
- Blocker
- Next action
- References

Credential/token/secret/private user data を記載しない。Handoff は branch ownership claim を解放しない。Resume worker は Issue/PR、Spec、latest main、branch HEAD、parallel reservations を再読する。
