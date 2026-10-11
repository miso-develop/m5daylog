# Task #87 Strategy 2 rework implementation plan

**Role:** implementation  
**Domain:** device  
**Task:** #87  
**PR:** #88

## Contract

Replace the superseded Strategy 3 disconnect/reconnect loop with the approved
explicit safe-eject shutdown lifecycle. Device/PC microSD ownership remains
exclusive and every ambiguous release signal fails closed.

## Implementation

1. Replace suspend/detach release authorization with TinyUSB MSC
   `START STOP UNIT(load_eject=1,start=0)`.
2. On explicit eject, logically disconnect immediately, then uninstall TinyUSB
   and delete the USB-owned storage object to prove deferred host I/O is zero.
   Do not rebuild APP storage.
3. Add NVS-backed `SHUTDOWN_ARMED` intent. Commit it only after USB quiescence.
4. Release M5Capsule HOLD and enter low-power shutdown. While USB remains,
   firmware stays functionally shut down; after USB removal HOLD=0 permits
   actual power-off.
5. Gate boot on persistent armed intent. Non-manual boots remain shut down.
   Manual WAKE (GPIO42) reacquires Device ownership, completes pending recovery,
   clears the armed intent durably, and starts a fresh recording session.
6. Preserve a narrow pending-RTC flush hook before armed-intent clear so Task
   #50 can retain exactly-once pending-event ordering during integration.
7. Keep APP→USB finalize/manifest/Device-FS-release publication gating intact.

## TDD / verification

- RED: executable host C tests require explicit eject, reject suspend/detach as
  authorization, prove teardown-before-storage-release, no APP remount, and NVS
  armed persistence/failure behavior.
- GREEN: implement the minimum production changes.
- Run repository test, security scan, and ESP-IDF v5.5.5 clean-build checks on
  the exact PR head.
- Physical release/shutdown/WAKE cycle evidence remains a Human Gate and is not
  claimed by this implementation.
