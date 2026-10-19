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

## Independent Review session / formal GitHub record

Independent Engineering / Security Reviewが必要な場合、**実装セッションとは別のAI会話・セッション**で実施する。前セッションの結論を承認根拠に転用せず、GitHubのcurrent exact PR HEAD、live base、diff、Task/Spec、PROJECT/SECURITY、検証status、parallel ownershipをfresh-readする。EngineeringとSecurityそれぞれの観点で具体的な根拠、blocking/nonblocking finding、未検証事項を記録する。単なるCI greenや実装者の自己申告を独立評価の代わりにしない。

- GitHub loginがPR authorと同一であっても、別AIセッションの独立性は否定しない。別GitHub account作成やplatform approvalの偽装を要求しない。
- PR author自身による`APPROVE`は不可。実質的なReview判定が揃い、権限が許す場合は、対象exact HEADとEngineering/Security disposition、証拠、limits、セッション独立性を本文に含め、正式なGitHub Pull Request Reviewを`COMMENT`で投稿する。
- 投稿後、GitHub Review ID / URL、reviewer login、state=`COMMENTED`、review commit SHA、現行PR HEADを再取得して照合する。Issue/PR Conversation commentはsubmitted Reviewではない。Draft/API/permission rejection時は`SUBMITTED`と主張せず、記録gateを未充足とする。
- Technical Engineering `CLEAR` / `SECURITY_CLEAR` とGitHub `APPROVED`は異なる。required approval、blocker、authentic checks、HEAD/baseの鮮度、exact-head/semantic merge safetyは独立したgateであり、`COMMENTED`で解除しない。
- 投稿・関連Issue/PR更新・HEAD/base変更後はfreshnessとReview対象一致を確認し、不一致または新たなriskがあれば再Reviewする。Security reviewが必要かは実際の変更リスクと`SECURITY.md`に従い、実質評価を省略しない。
