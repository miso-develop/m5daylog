---
name: loop-status
description: GitHub live state から ready / blocked / in-flight / handoff を read-only で集約する。
---

# Loop Status

Read-only で以下を確認する。

- open `[Task]` / Parent Spec / blockers
- open / Draft PR
- non-main Task branches
- latest main
- recent handoff comments

Ready は blockers が merge/close 済みで ownership conflict がない Task。Green-but-unmerged prerequisite は blocked のまま。Chat memory を progress source にしない。Status確認だけで branch/file を変更しない。
