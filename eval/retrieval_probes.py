"""Schema-retrieval probes: a question-like query and the table that should answer it.

Scored by ``probe_scores`` (top-1 / top-3 hit rate of SchemaIndex.search); the numbers are
written into the eval report. No API key needed.
"""

from __future__ import annotations

PROBES: list[tuple[str, str]] = [
    ("crash rate by firmware", "fct_firmware_daily"),
    ("average trip distance", "fct_trip"),
    ("GPS fix validity per minute", "fct_device_minute"),
    ("which firmware was released most recently", "dim_firmware"),
    ("event code severity", "dim_event_code"),
    ("region timezone", "dim_region"),
    ("weekend days", "dim_date"),
    ("LTE loss percentage per device per day", "fct_device_daily"),
    ("How many devices are in region us-west?", "dim_device"),
    ("firmware version as of a date for a device", "dim_device"),
    ("crash rate per region per day", "fct_device_daily"),
    ("percentage of fleet on firmware 1.1.0", "dim_device"),
    ("trips started on weekends", "fct_trip"),
    ("median trip distance by hardware model", "fct_trip"),
    ("which devices crashed", "fct_device_event"),
    ("board temperature percentile", "fct_device_minute"),
    ("drive hours per region", "fct_device_daily"),
    ("upgrade day", "dim_device"),
]


def probe_scores(index) -> dict:
    top1 = top3 = 0
    misses = []
    for q, want in PROBES:
        got = [h["table"].split(".")[-1] for h in index.search(q, 3)]
        top1 += got[0] == want
        top3 += want in got
        if got[0] != want:
            misses.append({"query": q, "expected": want, "got": got})
    n = len(PROBES)
    return {"n": n, "top1": top1, "top3": top3, "top1_pct": round(100 * top1 / n, 1),
            "top3_pct": round(100 * top3 / n, 1), "top1_misses": misses}
