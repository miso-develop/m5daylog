# Parallel Work Checklist

## Before selecting / claiming a Task

- [ ] latest `main` SHA を確認した
- [ ] open / Draft PR を確認した
- [ ] non-main Task branches を確認した
- [ ] Task blockers が merge / close 済みである
- [ ] exact-file overlap を確認した
- [ ] subsystem / ownership overlap を確認した
- [ ] protocol / schema / interface / security / build contract overlap を確認した
- [ ] unmerged prerequisite に依存していない
- [ ] ambiguity がない。ある場合は fail closed とした

## Claim

- [ ] canonical `task/<issue-number>` branch を observed latest main から作成した、または既存 claim を正しく resume した
- [ ] claim 後、first write 前に coordination state を再確認した

## During implementation

- [ ] scope expansion 前に relevant preflight を再実行した
- [ ] new shared dependency/lockfile/workflow/security boundary を無断で予約していない
- [ ] first meaningful commit 後に push / PR state を再確認した
- [ ] handoff が必要なら reservation surface を記録した

## Before merge

- [ ] exact PR head SHA を確認した
- [ ] latest main を確認した
- [ ] current open/Draft PR / branch reservation を再確認した
- [ ] file overlap と semantic overlap の両方を確認した
- [ ] main advancement の影響を評価した
- [ ] 必要な branch update / affected checks rerun を行った
- [ ] security review / required checks が exact head で green
