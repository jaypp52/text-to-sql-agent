# Claude Code baseline

Generated 2026-11-24T11:26:52+00:00 by `python -m eval.claude_code_baseline` · 50 labelled questions · platform warehouse · **Claude via Claude Code sessions using this agent's tools, not the API agent loop** (no token, cost or latency figures).

| Metric | Claude Code · haiku | Claude Code · sonnet | Claude Code · opus |
|---|---|---|---|
| Execution accuracy, all 50 | 86.0% | 94.0% | 90.0% |
| … answerable (44) | 84.1% | 93.2% | 88.6% |
| … easy | 100.0% | 100.0% | 100.0% |
| … medium | 73.3% | 93.3% | 80.0% |
| … hard | 78.6% | 85.7% | 85.7% |
| Abstention accuracy (6 unanswerable) | 100.0% | 100.0% | 100.0% |
| Valid SQL, first attempt | 100.0% | 100.0% | 100.0% |
| Questions that hit ≥ 1 SQL error | 0.0% | 0.0% | 0.0% |
| … of those recovered | n/a | n/a | n/a |
| Mean SQL attempts per question | 1.98 | 1.98 | 2.32 |
| Guard rejections | 0 | 0 | 0 |

Every wrong answer, with a hand-written diagnosis (`eval/claude_code_baseline_notes.json`):

| Question | Wrong for | Cause |
|---|---|---|
| h01 | haiku, sonnet, opus | returned every model, not the top 1 (haiku, sonnet); a different, day-weighted metric from fct_firmware_daily (opus) |
| h09 | haiku | included 2026-01-01 with a NULL change (haiku) |
| h10 | haiku, sonnet, opus | ambiguous question: three different readings of 'devices that upgraded' and 'days fully on'; flagged for gold review |
| m05 | opus | opus excluded trips whose start was never loaded (started_at IS NULL); arguably more correct than the gold; flagged for gold review |
| m08 | haiku, opus | rounded to 2 decimals although the question did not ask; strict 1e-6 comparison counts it wrong |
| m09 | haiku | listed every day instead of the single top day (haiku) |
| m10 | haiku, opus | rounded to 2 decimals although the question did not ask; strict 1e-6 comparison counts it wrong |
| m11 | haiku, sonnet | returned every model ranked instead of the top 1 |
