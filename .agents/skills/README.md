# Agent Skills

必要な場面でだけ該当 Skill を読む。全 Skill を毎 iteration に読み込まない。

| Skill | Trigger |
| --- | --- |
| `wayfinder` | 大きく曖昧な work を decision map にする |
| `to-spec` | settled decisions から implementation-independent Spec を作る |
| `to-tickets` | Spec を vertical Task に分解する |
| `implement` | ready Task を branch -> test/review -> PR -> merge へ進める |
| `handoff` | 未完了 work を別 session / agent に引き継ぐ |
| `loop-status` | GitHub live state から ready/blocked/in-flight を集約する |
| `codebase-design` | module boundary / interface / seam を設計する |
| `tdd` | behavior を red -> green で実装する |
| `code-review` | requirement / security / quality / parallel merge safety を review する |
| `local-env-config` | `.env` / local environment / credential input boundary を変更する |
| `security-review` | token/secret/private-data/network/logging boundary を変更する |

Security-sensitive work では Skill より先に `SECURITY.md` を適用する。
