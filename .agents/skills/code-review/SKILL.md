---
name: code-review
description: Diff を requirement / security / engineering / parallel merge safety の別軸で review する。
---

# Code Review

1. Exact PR head / merge-base と current Task / Spec を確定する。
2. Requirement compliance: missing AC、unrequested behavior、regression、contract weakening を確認する。
3. Security/privacy: secret exposure、private-data log/upload、unsafe defaults、credential lifetime、dependency/network change を確認する。
4. Engineering: correctness、error handling、state ownership、test quality、maintainability を確認する。
5. Parallel safety: latest main、open PR/branches、file + semantic/shared-contract overlap を確認する。
6. Finding は severity 順に、specific location / failure mode / conflicting requirement を示す。

Git mergeable であることだけを safe integration の証拠にしない。Security regression は functional tests が green でも blocking defect。
