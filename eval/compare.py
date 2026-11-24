"""Execution accuracy: does the predicted SQL return the same result set as the gold SQL?

Rules (from the plan):
- column names are ignored; columns are compared by position. When the prediction has
  MORE columns than the gold result, the gold columns may be found among them in any
  order (an extra id or label column does not make an answer wrong); fewer columns is a
  mismatch;
- row order is ignored unless ``order_matters``;
- floats (and DECIMALs) compare with relative tolerance 1e-6; NULL equals NULL;
- duplicate rows count (multiset comparison).

Where it can give false positives: two different queries can return the same rows on
this data by coincidence (e.g. a wrong filter that happens to exclude nothing). A small
result such as a single count is the most exposed; the failure analysis spot-checks those.
"""

from __future__ import annotations

import datetime as dt
import decimal
import itertools
import math
import re
from dataclasses import dataclass

REL_TOL = 1e-6
ABS_TOL = 1e-9

ABSTAIN = re.compile(
    r"(can ?not|can't|cannot|unable|not (be )?(possible|answerable|available|present|"
    r"recorded|tracked|stored|captured|included|contain)|no (data|information|column|table|"
    r"record)|does(n't| not) (contain|have|include|track|store|record)|isn't (available|"
    r"tracked|recorded|stored)|not in the (warehouse|data|schema)|outside the (scope|data))",
    re.IGNORECASE,
)


@dataclass
class Match:
    ok: bool
    reason: str = ""
    extra_columns: bool = False


def norm(v):
    """Normalise a value so that equal results compare equal across types."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, int | float):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, str):
        # dates that came through JSON as ISO strings
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?", v):
            return v.replace("T", " ")
        return v
    return str(v)


def vals_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    if isinstance(a, float) != isinstance(b, float):
        return False
    return a == b


def _key(v):
    """Sort key that groups values which compare equal (floats rounded to ~7 digits)."""
    if v is None:
        return (0, "")
    if isinstance(v, float):
        return (1, f"{v:.7g}")
    return (2, str(v))


def _num_key(v):
    if v is None:
        return (0, 0.0, "")
    if isinstance(v, float):
        return (1, v, "")
    return (2, 0.0, str(v))


def rows_equal(gold: list[tuple], pred: list[tuple], order_matters: bool) -> bool:
    if len(gold) != len(pred):
        return False
    if not order_matters:
        gold = sorted(gold, key=lambda r: [_key(v) for v in r])
        pred = sorted(pred, key=lambda r: [_key(v) for v in r])
    return all(
        all(vals_equal(a, b) for a, b in zip(g, p, strict=True))
        for g, p in zip(gold, pred, strict=True)
    )


def compare(gold_rows: list, pred_rows: list, order_matters: bool = False,
            max_permutations: int = 50_000) -> Match:
    g = [tuple(norm(v) for v in r) for r in gold_rows]
    p = [tuple(norm(v) for v in r) for r in pred_rows]
    if len(g) != len(p):
        return Match(False, f"row count differs: gold {len(g)}, predicted {len(p)}")
    if not g:
        return Match(True, "both empty")
    gc, pc = len(g[0]), len(p[0])
    if pc < gc:
        return Match(False, f"predicted result has {pc} columns, gold has {gc}")
    if pc == gc and rows_equal(g, p, order_matters):
        return Match(True)

    # Look for the gold columns among the predicted ones (any order). Prune with
    # per-column multiset equality before trying joint assignments.
    def col(rows, i):
        return [r[i] for r in rows]

    def col_match(gi, pi) -> bool:
        a, b = col(g, gi), col(p, pi)
        if order_matters:
            return all(vals_equal(x, y) for x, y in zip(a, b, strict=True))
        a, b = sorted(a, key=_num_key), sorted(b, key=_num_key)
        return all(vals_equal(x, y) for x, y in zip(a, b, strict=True))

    cands = [[pi for pi in range(pc) if col_match(gi, pi)] for gi in range(gc)]
    if any(not c for c in cands):
        return Match(False, "values differ")
    tried = 0
    for combo in itertools.product(*cands):
        if len(set(combo)) != gc:
            continue
        tried += 1
        if tried > max_permutations:
            break
        proj = [tuple(r[i] for i in combo) for r in p]
        if rows_equal(g, proj, order_matters):
            return Match(True, "matched on a subset/permutation of columns",
                         extra_columns=pc > gc or list(combo) != list(range(gc)))
    return Match(False, "values differ")


def is_abstention(sql: str, answer: str) -> bool:
    """Correct behaviour on an unanswerable question: empty SQL and an answer that says so."""
    return not (sql or "").strip() and bool(ABSTAIN.search(answer or ""))
