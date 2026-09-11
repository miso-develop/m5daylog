---
name: security-review
description: Credential/token/private-data/network/logging/storage boundary を変更する diff を fail-closed で review する。
---

# Security Review

`SECURITY.md` を canonical policy とする。

Review axes:

- secret enters where / from whom / for how long
- storage and lifetime
- log/error/telemetry/trace exposure
- command line / environment / URL / IPC exposure
- network destinations and external dependencies
- raw audio / transcript / daily-log privacy boundary
- CI permissions / action pinning / artifact content
- synthetic testability
- failure path / cleanup / rotation response

Unknown sensitivity は sensitive として扱う。Security control を convenience のため一時無効化する提案は reject する。Finding に actual secret value をコピーしない。
