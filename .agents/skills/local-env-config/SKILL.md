---
name: local-env-config
description: `.env` / local environment variable / dotenv loader / credential input boundary を安全に扱う。
---

# Local Environment Configuration

## Classify first

各 setting を canonical project configuration / local machine configuration / secret credential のどれかに分類する。

## Tracked template is value-free

Tracked `.env.example` は利用可能 key を宣言するだけで、すべて `KEY=` とする。Default/example/host/path/token 等の concrete value を入れない。Canonical project default は manifest/config/docs 等の authoritative source に置く。

## Local values stay untracked

Concrete local value を書く前に target file が Git ignored かつ untracked であることを確認する。`.gitignore` を後から追加しても過去 commit は消えない。

`.env` は ignored plaintext file であり secure secret store ではない。Credential は可能なら OS credential store / dedicated secret manager / CI secret facility を使う。

## Loader contract

- allowlist supported keys
- reject unknown / duplicate keys
- validate required values / formats
- inherited environment precedence を明示
- secret value を log/error に含めない
- command-line argument や URL に secret を移さない

## Exposure

Tracked concrete secret が見つかったら削除だけで済ませない。Value を再掲せず、まず authoritative service で revoke/rotate し、Git history / PR / Actions log / artifact 等を調査する。
