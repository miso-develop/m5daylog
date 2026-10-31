# M5Daylog Project Charter

## Purpose

M5Stack device で取得した音声を Windows PC に同期し、local-first で STT、speaker diarization、日次ログ生成まで行う PoC を構築する。

詳細な product requirements / decisions / specs / tasks は GitHub Issues を正とする。Project-wide な長寿命 invariant だけを本ファイルに置く。

## Repository source of truth

- Project Map: #1
- Issue operating model: #33
- Repository structure / responsibility boundary: #34
- Feature-level requirements: linked `[Decision]` / `[Spec]` Issues
- Implementation progress: `[Task]` Issues / Pull Requests / merged commits

Closed Issue の内容だけを current implementation contract とみなさない。将来も必要な知識は code、configuration、durable docs、tests/checks へ昇格する。

## Project-wide invariants

- PC 側の音声処理は local-first を基本とする。
- Device / PC の boundary contract は `contracts/` で明示し、Integration ownership とする。
- Raw audio、transcript、daily log、speaker metadata は private user data として扱う。
- Account credential、token、secret、private key、session material の保護は全機能要件より優先する。
- Security handling の詳細は `SECURITY.md` を正とし、他の文書と衝突する場合はより厳しい rule を適用する。
- 実装は Issue-driven Loop Engineering に従い、ready な `[Task]` を1 iterationにつき1件だけ扱う。
- 複数 worker の並列実装は `agent/PARALLEL-WORK.md` の preflight / reservation / exact-head merge safety に従う。
- Repository-owned tests / static checks / CI を verification evidence とする。未導入の check を成功したものとして扱わない。
- Review への引継ぎは exact HEAD の変更範囲について、関連 Acceptance Criteria、実行済み検証、既知 blocking finding の解消根拠と未検証範囲を GitHub に残す。状態遷移・永続化・競合・外部入力は、実際の production decision/transaction path と代表的な異常系を検証する。CI 成功だけで独立 Review や実機 Human Gate を代替しない。
- 限定差分の Engineering / Security clear と Task 全体の完成・マージ・実機実行許可を区別する。Integration が既存判定を再利用するときは、現在の HEAD/base、変更の意味、依存・所有権・Security/Acceptance 前提と必要な checks を再確認し、影響範囲を再評価する。安全要件・独立性・Human Gate・exact-head merge gate は緩和しない。

## Top-level ownership

- `firmware/`: Device Agent
- `pc/`: PC Agent
- `contracts/`: Integration Agent
- `fixtures/`: synthetic/public test data only; relevant owner + security review
- cross-boundary protocol / schema / security contract: Integration Agent

Product-specific ownershipは各 Task / Spec の明示を優先する。
