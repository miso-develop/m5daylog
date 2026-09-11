---
name: wayfinder
description: Large or ambiguous work を decision map に分解する。
---

# Wayfinder

1. Desired outcome / scope / constraints / current evidence を確認する。
2. 実装案ではなく、結論が必要な question を列挙する。
3. Question 間の dependency を整理し、独立なものは並行 Decision にする。
4. GitHub `[Map]` に outcome、boundaries、questions、exit condition を記録する。
5. 各 question は `[Decision]` Issue で evidence/options/conclusion/consequences を扱う。
6. Security/privacy question は他の利便性判断から独立させ、`SECURITY.md` の invariant を下限とする。

Settled decision が揃うまで production Task を推測して作らない。
