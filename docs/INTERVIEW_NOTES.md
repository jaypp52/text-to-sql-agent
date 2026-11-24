# Interview notes: Text-to-SQL Agent

These are short answers to the plan's interview questions, with pointers into the code.
Where an answer needs a measured number, it says which field of
`eval/results/latest.json` to quote; fill those in after the first real `make eval`.

## Why a manual tool loop instead of a framework?

- **Visibility.** Every step shows up in the trace: each API call with its stop reason
  and token usage, each tool call with its input, output and error flag, and each nudge.
  In an interview I can point at `agent/loop.py` and walk through one question.
- **Control over the parts that matter here.** These are easy to get subtly wrong
  inside a framework:
  - check `stop_reason` before reading content (refusal, `max_tokens`, `end_turn`
    without `submit_answer`);
  - answer every `tool_use` block in one user message;
  - put a hard limit on failed SQL attempts;
  - re-execute the submitted SQL;
  - handle the server-side fallback's echo rule.
- **Testability.** The client can be swapped out. `tests/test_loop.py` scripts the
  error → retry → success path, parallel tool calls, refusals, the error limit and the
  fallback echo rule. `tests/test_sdk_wire.py` runs the real SDK against a fake HTTP
  endpoint to check what goes over the wire.
- **Trade-off.** It is more code to own than the SDK's tool runner (which is the better
  default for most agents). But it is about 300 lines, and every one of them is tested.

## Why is the guard layered? Isn't `read_only=True` enough?

No. `read_only` only blocks writes to the database file.
`test_read_only_alone_is_not_enough` shows that a plain read-only connection still
allows:
- reading any file (`read_csv('/etc/passwd')`);
- running SQL from a string (`query('…')`);
- creating temp tables, running `PRAGMA` and changing `search_path`.

Extensions and settings changes aren't "writes" either. The layers:

1. `read_only` stops writes to the warehouse.
2. External access is disabled at connect time and the configuration is then
   **locked**, so SQL can't switch it back on. This stops files, URLs, `COPY TO`,
   `ATTACH` and extension installs. `LOCKDOWN` in `eval/attacks.py` proves this layer
   holds with the guard bypassed.
3. An AST allow-list checks the shape of the query: exactly one SELECT; no write
   hidden in a CTE; no file, URL or SQL-string functions; only the 10 governed mart
   tables.
4. Limits: `LIMIT`, a 50-row cap to the model, a timeout and a memory limit, against
   runaway cost even for a "safe" query.

Each layer covers a different failure mode. Any single one can have a gap (a new
DuckDB function, a sqlglot misparse), and a gap in one layer is caught by another.

## Execution accuracy vs exact-match SQL accuracy

- **Exact match** compares SQL text, so it marks correct answers wrong whenever the
  query is written differently: aliases, join order, a CTE instead of a subquery,
  `count(DISTINCT x)` vs `GROUP BY`.
- **Execution accuracy** runs both queries and compares the result sets
  (`eval/compare.py`):
  - column names are ignored, and extra columns are allowed;
  - row order is ignored unless the question implies an order;
  - floats match within a relative tolerance of 1e-6;
  - duplicates count.
- **False positives.** A wrong query can return the right rows on this data by
  coincidence. Examples: a filter that excludes nothing here, or a join that happens
  not to duplicate rows. Single-number answers are the most exposed. Mitigations:
  - questions whose wrong readings give different numbers;
  - checking the trace for every hard question in the failure analysis;
  - optionally, running both queries on a second database (the seed) as a
    perturbation check.
- **False negatives.** A defensible alternative reading scores as wrong: rounding,
  `quantile_disc` vs `median`, or device-day vs minute-weighted averages. That's why
  questions state the definition when it matters, for example "average of the
  device-day lte_loss_pct, rounded to 2 decimals".

## What did the failure analysis show, and what changed because of it?

_Fill in after the first real run, from `eval/results/failures.md` and the
`errors_by_kind` and `self_correction` fields of `latest.json`._

Things to look for, because the questions were built to probe them:
- averaging daily rates instead of `sum(crash_count) / sum(device_days)` (m01, m02, h02,
  h11);
- forgetting that `dim_device` is SCD2: counting rows instead of distinct devices, or
  ignoring the validity window (e15, h03–h05, h10);
- date windows: including the 2026-11-23 live data when the question says January;
- answering an unanswerable question with a plausible-looking 0.

Each fix goes into the system prompt, the tool descriptions or the dbt column
descriptions (the platform's `_marts.yml`), never into the eval.

## When does schema retrieval matter?

- At 10 tables, every schema document fits in the prompt. The `no_retrieval` ablation
  does exactly that, and the cached prefix makes it cheap to repeat.
- Compare `configs.main` with `configs.no_retrieval` in `latest.json`: accuracy, mean
  input tokens, cache-read share, cost and latency.
- Retrieval pays off when the catalogue grows to hundreds of tables:
  - the prompt would grow linearly, and every edit to the docs would invalidate the
    cache;
  - the model would be distracted by look-alike tables.

  Then you retrieve, and add embeddings or column-level retrieval on top of BM25.
- The probe set (`retrieval_probes` in the report) shows the current lexical
  retriever's top-1 and top-3 hit rates.

## How would business users trust the answers?

- **Show the work.** Every answer comes with the SQL, its result table and the
  assumptions the model made (date window, metric definition), plus a trace of what it
  looked at.
- **Governed layer only.** It queries dbt marts that have tests, and it uses metric
  definitions from the dbt docs, not ones the model invents.
- **Refuses rather than guesses** when the data doesn't exist. Abstention accuracy is
  a reported metric.
- **Read-only by construction**, with every guard rejection logged.
- **Measured.** Execution accuracy by difficulty, on a labelled set a human checked.
- **Next steps.** Show row counts and freshness next to each answer, require human
  review of answers whose SQL touches more than N tables, and log questions and answers
  for review.
