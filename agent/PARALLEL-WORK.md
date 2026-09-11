# Parallel Implementation Coordination

複数 chat / agent / human worker が同一 repository を同時に変更できることを前提に、GitHub の live state を coordination source とする。

## Mandatory preflight

Implementation/current-truth change を開始する前に最新状態を取得する。

1. latest `main` commit
2. open / Draft PR と head branch / changed files
3. Task に合理的に対応する non-main branch
4. open Task と blockers
5. 自分が変更予定の file / subsystem / shared contracts
6. Security-sensitive surface の ownership

Chat memory や「別 worker はたぶん止まっている」という推測で ownership を判断しない。

## Reservation model

Open/Draft PR または Task に対応する unfinished branch は、少なくとも以下を reservation しているとみなす。

- exact changed files
- coherent subsystem / ownership boundary
- protocol / schema / public interface
- persisted format / state machine
- dependency / lockfile contract
- build / release / CI workflow contract
- security / privacy boundary

Changed-file overlap がなくても同じ shared contract を別々の解釈で変更するなら hard conflict である。

## Parallel eligibility gate

別 Task を並行開始できるのは次をすべて満たす場合だけ。

- Task は ready で blockers が merge/close 済み。
- Existing worker が同じ Task を claim していない。
- Planned exact files に conflict がない。
- Planned subsystem / semantic ownership に conflict がない。
- Shared protocol/schema/interface/security/build contract に conflict がない。
- Unmerged branch の behavior を prerequisite としない。
- Conflict が生じた場合に安全に再評価できる独立境界がある。

不明確なら fail closed で並列化しない。

## Atomic Task claim

Preflight 通過後、meaningful implementation 前に observed latest `main` から remote `task/<issue-number>` を作る。Ref が既に存在した場合は ownership race とみなし、新しい branch で迂回せず停止して既存 state を調査する。

Branch 作成後、first meaningful write 前に open PR / branches / latest main を再読する。

## Scope expansion

実装中に originally planned surface を超えて shared file/subsystem/contract/dependency/workflow/security boundary を変更する必要が出たら、書き込む前に relevant preflight を再実行する。Scope creep を「小さい変更」として黙って予約外へ広げない。

## Push / PR visibility

First meaningful commit 後は current GitHub state を再確認し、可能なら早期 Draft PR で Task ownership / planned surface を可視化する。PR description に Parent Spec、Task、change surface、blockers を記載する。

## Main advancement

作業中に `main` が進んだ場合、text conflict だけでなく semantic/dependency/security overlap を評価する。Material overlap があれば latest main を取り込み、affected checks を再実行する。

## Exact-head merge preflight

Merge 直前に:

1. exact PR head SHA を pin
2. latest main を解決
3. branch base / last safety check 以降の relevant merges を確認
4. open/Draft PR / branch reservations を再確認
5. file + semantic + shared-contract overlap を評価
6. 必要なら branch update / rerun checks
7. reviewed head と merge target が変わっていないことを確認

GitHub が `mergeable` と判定することだけでは semantic safety の証明にならない。

## Abandoned claims

古い `task/<issue>` branch を見つけても自動削除・再利用しない。Issue/PR/commit/handoff を確認し、owner が abandon した根拠を明示してから cleanup する。判断不能なら保持して handoff / user decision を求める。

## Security exception is forbidden

Security rule は並列作業の障害を理由に bypass しない。Secret/private-data exposure の疑いがある worker は通常 feature work を停止し、`SECURITY.md` の incident response を優先する。
