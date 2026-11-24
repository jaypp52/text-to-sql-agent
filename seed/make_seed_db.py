"""Build a small synthetic warehouse with the SAME schema as the telemetry platform's marts.

    python -m seed.make_seed_db [--devices 60] [--out seed/warehouse_seed.duckdb]

Used by CI and for working before the platform exists. Column names, order and DuckDB
types come from seed/dbt_target/catalog.json (exported from the platform's dbt docs), so
the schema cannot drift silently; tests/test_schema_index.py checks it. The data is
deterministic (fixed seed) and covers 2026-01-01..2026-01-07, the same window as the
platform's backfill: 3 hardware models, 5 regions, a 1.0.0 -> 1.1.0 firmware rollout
from 2026-01-02, with TX-200 crashing more often on 1.1.0 and us-west losing LTE more.
The two aggregate marts are derived with the same SQL as the platform's dbt models.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import tempfile
from pathlib import Path

import duckdb

from agent.config import SEED_DBT_TARGET, SEED_WAREHOUSE

START = dt.date(2026, 1, 1)
DAYS = 7
REGIONS = [
    ("us-west", "US West", "America/Los_Angeles", 37.77, -122.42),
    ("us-east", "US East", "America/New_York", 40.71, -74.0),
    ("eu-central", "EU Central", "Europe/Berlin", 50.11, 8.68),
    ("in-west", "India West", "Asia/Kolkata", 19.07, 72.88),
    ("ap-southeast", "AP Southeast", "Asia/Singapore", 1.35, 103.82),
]
EVENT_CODES = [
    ("CAM_CRASH", "camera", "error", "Camera pipeline process exited unexpectedly"),
    ("REBOOT", "system", "warning", "Device rebooted (after a crash or spontaneously)"),
    ("LOW_MEMORY", "system", "warning", "Free memory dropped below 25% of the model's budget"),
    ("OVERHEAT", "thermal", "critical", "Board temperature exceeded 85 C"),
    ("FW_UPDATE", "firmware", "info", "Over-the-air firmware update applied"),
    ("SD_CARD_ERROR", "storage", "error", "Write to local storage failed"),
]
FIRMWARE = [("1.0.0", dt.date(2025, 6, 2), 1, 0, 0, False),
            ("1.1.0", dt.date(2026, 1, 2), 1, 1, 0, True)]
MODELS = [("TX-100", 0.45, 512), ("TX-200", 0.33, 384), ("TX-300", 0.22, 768)]
# CAM_CRASH probability per drive minute (~5 per 100 device-days; TX-200 x2.7 on 1.1.0)
CRASH_P = {("TX-100", "1.0.0"): 0.0011, ("TX-100", "1.1.0"): 0.0011,
           ("TX-200", "1.0.0"): 0.0010, ("TX-200", "1.1.0"): 0.0027,
           ("TX-300", "1.0.0"): 0.0012, ("TX-300", "1.1.0"): 0.0011}
FAR = dt.datetime(9999, 12, 31)


def md5(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()  # noqa: S324


def build_rows(n_devices: int, seed: int) -> dict[str, list[tuple]]:
    rnd = random.Random(seed)
    rollout_start = dt.datetime(2026, 1, 2)
    devices = []
    for i in range(1, n_devices + 1):
        r = rnd.random()
        model = "TX-100" if r < 0.45 else "TX-200" if r < 0.78 else "TX-300"
        region = REGIONS[(i - 1) % len(REGIONS)][0]
        install = dt.date(2025, 1, 1) + dt.timedelta(days=rnd.randrange(0, 360))
        # ~70% of the fleet upgrades during the window
        upgrade = (rollout_start + dt.timedelta(days=rnd.randrange(0, 5),
                                                minutes=rnd.randrange(6 * 60, 20 * 60))
                   if rnd.random() < 0.7 else None)
        bad_lte = rnd.random() < 0.08
        devices.append(dict(id=f"dev-{i:06d}", model=model, region=region, install=install,
                            upgrade=upgrade, bad_lte=bad_lte))

    dim_device, minutes, trips, events = [], [], [], []
    for d in devices:
        v1_from = dt.datetime.combine(d["install"], dt.time())
        k1 = md5(d["id"], v1_from)
        if d["upgrade"]:
            k2 = md5(d["id"], d["upgrade"])
            dim_device += [(k1, d["id"], d["model"], d["region"], d["install"], "1.0.0",
                            v1_from, d["upgrade"], False),
                           (k2, d["id"], d["model"], d["region"], d["install"], "1.1.0",
                            d["upgrade"], FAR, True)]
        else:
            dim_device.append((k1, d["id"], d["model"], d["region"], d["install"], "1.0.0",
                               v1_from, FAR, True))

        def as_of(ts, d=d, k1=k1):
            if d["upgrade"] and ts >= d["upgrade"]:
                return "1.1.0", md5(d["id"], d["upgrade"])
            return "1.0.0", k1

        if d["upgrade"]:
            fw, key = as_of(d["upgrade"])
            events.append((md5("ev", d["id"], d["upgrade"]), d["id"], key, d["upgrade"],
                           d["upgrade"].date(), "FW_UPDATE", "info", "updated to 1.1.0",
                           fw, d["model"], d["region"], False))
        lat0, lon0 = next((r[3], r[4]) for r in REGIONS if r[0] == d["region"])
        for day in range(DAYS):
            date = START + dt.timedelta(days=day)
            if rnd.random() < 0.06:  # parked all day
                continue
            for _ in range(rnd.choice([1, 1, 2])):
                start = dt.datetime.combine(date, dt.time()) + dt.timedelta(
                    minutes=rnd.randrange(5 * 60, 21 * 60))
                length = rnd.randrange(12, 70)
                dist = 0.0
                for m in range(length):
                    ts = start + dt.timedelta(minutes=m)
                    fw, key = as_of(ts)
                    speed = max(0.0, rnd.gauss(42, 15))
                    dist += speed / 60
                    temp = round(rnd.gauss(55 if d["model"] != "TX-300" else 60, 8), 1)
                    lost_p = 0.30 if d["bad_lte"] else (0.07 if d["region"] == "us-west" else 0.02)
                    registered = rnd.random() > lost_p * 0.6
                    ip = registered and rnd.random() > lost_p * 0.4
                    fix = rnd.random() > 0.03
                    budget = next(b for mm, _, b in MODELS if mm == d["model"])
                    minutes.append((
                        md5(d["id"], ts), d["id"], ts, ts.date(), key, d["model"], d["region"],
                        fw, True, round(rnd.uniform(15, 85), 1),
                        int(budget * rnd.uniform(0.2, 0.8)), temp, round(rnd.uniform(20, 95), 1),
                        round(rnd.gauss(-95, 8), 1), round(rnd.gauss(12, 5), 1), registered, ip,
                        not (registered and ip),
                        round(lat0 + rnd.uniform(-0.3, 0.3), 5) if fix else None,
                        round(lon0 + rnd.uniform(-0.3, 0.3), 5) if fix else None,
                        round(speed, 1) if fix else None, rnd.randrange(4, 14) if fix else 0, fix,
                    ))
                    if rnd.random() < CRASH_P[(d["model"], fw)]:
                        events.append((md5("crash", d["id"], ts), d["id"], key, ts, ts.date(),
                                       "CAM_CRASH", "error", "camera pipeline exited", fw,
                                       d["model"], d["region"], rnd.random() < 0.02))
                        rb = ts + dt.timedelta(seconds=40)
                        events.append((md5("reboot", d["id"], ts), d["id"], key, rb, rb.date(),
                                       "REBOOT", "warning", "rebooted after crash", fw,
                                       d["model"], d["region"], False))
                    if temp > 75 and rnd.random() < 0.3:
                        events.append((md5("hot", d["id"], ts), d["id"], key, ts, ts.date(),
                                       "OVERHEAT", "critical", f"board at {temp} C", fw,
                                       d["model"], d["region"], False))
                    if rnd.random() < 0.002:
                        events.append((md5("mem", d["id"], ts), d["id"], key, ts, ts.date(),
                                       "LOW_MEMORY", "warning", "free memory low", fw,
                                       d["model"], d["region"], False))
                    if rnd.random() < 0.0003:
                        events.append((md5("sd", d["id"], ts), d["id"], key, ts, ts.date(),
                                       "SD_CARD_ERROR", "error", "write failed", fw,
                                       d["model"], d["region"], False))
                end = start + dt.timedelta(minutes=length)
                complete = rnd.random() > 0.03
                fw, key = as_of(start)
                trips.append((
                    md5("trip", d["id"], start), d["id"], key, start, end if complete else None,
                    date, float(length) if complete else None,
                    round(dist, 2) if complete else None,
                    round(dist / (length / 60), 2) if complete else None,
                    fw, d["model"], d["region"], complete,
                ))
    return {"dim_device": dim_device, "fct_device_minute": minutes, "fct_trip": trips,
            "fct_device_event": events}


DERIVED = {
    "dim_region": "select * from raw_dim_region",
    "dim_event_code": "select * from raw_dim_event_code",
    "dim_firmware": "select * from raw_dim_firmware",
    "dim_date": """
        select d::date as date_day, year(d) as year, month(d) as month,
               day(d) as day_of_month, isodow(d) as iso_day_of_week,
               dayname(d) as day_name, isodow(d) in (6, 7) as is_weekend, weekofyear(d) as iso_week
        from range(date '2025-01-01', date '2028-01-01', interval 1 day) t(d)""",
    "dim_device": "select * from raw_dim_device",
    "fct_device_minute": "select * from raw_fct_device_minute",
    "fct_trip": "select * from raw_fct_trip",
    "fct_device_event": "select * from raw_fct_device_event",
    # Same logic as the platform's models/marts/fct_device_daily.sql
    "fct_device_daily": """
        with minutes as (
            select device_id, event_date, count(*) as drive_minutes,
                   count_if(lte_lost) as lte_loss_minutes, count_if(fix_valid) as gps_valid_minutes,
                   cast(round(avg(cast(temp_c as decimal(6, 1))), 2) as decimal(6, 2)) as avg_temp_c,
                   max(temp_c) as max_temp_c,
                   arg_max(firmware_version, minute_ts) as firmware_version,
                   arg_max(device_key, minute_ts) as device_key,
                   count(distinct firmware_version) > 1 as firmware_changed
            from marts.fct_device_minute group by device_id, event_date),
        events as (
            select device_id, event_date, count(*) as device_event_count,
                   count_if(event_code = 'CAM_CRASH') as crash_count,
                   count_if(event_code = 'REBOOT') as reboot_count,
                   count_if(event_code = 'OVERHEAT') as overheat_count,
                   arg_max(firmware_version, event_ts) as firmware_version,
                   arg_max(device_key, event_ts) as device_key
            from marts.fct_device_event group by device_id, event_date),
        trips as (
            select device_id, trip_date as event_date, count(*) as trip_count,
                   sum(cast(distance_km as decimal(12, 2))) as trip_distance_km
            from marts.fct_trip where is_complete group by device_id, trip_date),
        keys as (select device_id, event_date from minutes
                 union select device_id, event_date from events),
        registry as (select distinct device_id, hardware_model, region_id from marts.dim_device)
        select k.device_id, k.event_date, coalesce(m.device_key, e.device_key) as device_key,
               r.hardware_model, r.region_id,
               coalesce(m.firmware_version, e.firmware_version) as firmware_version,
               coalesce(m.firmware_changed, false) as firmware_changed,
               coalesce(m.drive_minutes, 0) as drive_minutes,
               coalesce(m.lte_loss_minutes, 0) as lte_loss_minutes,
               cast(round(100.0 * (m.lte_loss_minutes) / nullif(m.drive_minutes, 0), 2)
                    as decimal(9, 2)) as lte_loss_pct,
               cast(round(100.0 * (m.gps_valid_minutes) / nullif(m.drive_minutes, 0), 2)
                    as decimal(9, 2)) as gps_valid_pct,
               m.avg_temp_c, m.max_temp_c,
               coalesce(t.trip_count, 0) as trip_count,
               coalesce(t.trip_distance_km, 0) as trip_distance_km,
               coalesce(e.device_event_count, 0) as device_event_count,
               coalesce(e.crash_count, 0) as crash_count,
               coalesce(e.reboot_count, 0) as reboot_count,
               coalesce(e.overheat_count, 0) as overheat_count
        from keys k
        left join minutes m on k.device_id = m.device_id and k.event_date = m.event_date
        left join events e on k.device_id = e.device_id and k.event_date = e.event_date
        left join trips t on k.device_id = t.device_id and k.event_date = t.event_date
        left join registry r on k.device_id = r.device_id""",
    # Same logic as the platform's models/marts/fct_firmware_daily.sql
    "fct_firmware_daily": """
        with segments as (
            select device_id, event_date, firmware_version, hardware_model,
                   count(*) as drive_minutes, count_if(lte_lost) as lte_loss_minutes
            from marts.fct_device_minute
            group by device_id, event_date, firmware_version, hardware_model),
        device_days as (
            select firmware_version, hardware_model, event_date,
                   count(distinct device_id) as device_count, count(*) as device_days,
                   sum(drive_minutes) as drive_minutes,
                   cast(round(avg(cast(round(100.0 * lte_loss_minutes / nullif(drive_minutes, 0), 2)
                        as decimal(9, 2))), 2) as decimal(9, 2)) as mean_lte_loss_pct
            from segments group by firmware_version, hardware_model, event_date),
        crashes as (
            select firmware_version, hardware_model, event_date, count(*) as crash_count
            from marts.fct_device_event where event_code = 'CAM_CRASH'
            group by firmware_version, hardware_model, event_date)
        select d.firmware_version, d.hardware_model, d.event_date, d.device_count, d.device_days,
               d.drive_minutes, coalesce(c.crash_count, 0) as crash_count,
               round(100.0 * coalesce(c.crash_count, 0) / d.device_days, 3)
                   as crash_rate_per_100_device_days,
               d.mean_lte_loss_pct
        from device_days d left join crashes c
          on d.firmware_version = c.firmware_version and d.hardware_model = c.hardware_model
         and d.event_date = c.event_date""",
}
ORDER = ["dim_region", "dim_event_code", "dim_firmware", "dim_date", "dim_device",
         "fct_device_minute", "fct_trip", "fct_device_event", "fct_device_daily",
         "fct_firmware_daily"]


def mart_columns(catalog_path: Path) -> dict[str, list[tuple[str, str]]]:
    cat = json.loads(catalog_path.read_text())
    out = {}
    for node in cat["nodes"].values():
        if node["metadata"]["schema"] != "marts":
            continue
        cols = sorted(node["columns"].values(), key=lambda c: c["index"])
        out[node["metadata"]["name"]] = [(c["name"], c["type"]) for c in cols]
    return out


def build(out: Path, n_devices: int = 60, seed: int = 7) -> dict[str, int]:
    schema = mart_columns(SEED_DBT_TARGET / "catalog.json")
    rows = build_rows(n_devices, seed)
    rows["raw_dim_region"] = REGIONS
    rows["raw_dim_event_code"] = EVENT_CODES
    rows["raw_dim_firmware"] = FIRMWARE
    out.parent.mkdir(parents=True, exist_ok=True)
    for p in (out, out.with_suffix(out.suffix + ".wal")):
        if p.exists():
            p.unlink()
    con = duckdb.connect(str(out))
    con.execute("create schema marts")
    counts = {}
    with tempfile.TemporaryDirectory() as tmp:
        for table in ORDER:
            cols = schema[table]
            raw = f"raw_{table}"
            data = rows.get(raw) or rows.get(table)
            if data is not None:
                path = Path(tmp) / f"{table}.csv"
                with path.open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow([c for c, _ in cols])
                    for r in data:
                        w.writerow(["" if v is None else v for v in r])
                types = ", ".join(f"'{c}': '{t}'" for c, t in cols)
                con.execute(f"create temp table {raw} as select * from read_csv('{path}', "
                            f"header=true, columns={{{types}}}, nullstr='')")
            select = ", ".join(f'cast("{c}" as {t}) as "{c}"' for c, t in cols)
            con.execute(f"create table marts.{table} as select {select} "
                        f"from ({DERIVED[table]}) order by all")
            counts[table] = con.execute(f"select count(*) from marts.{table}").fetchone()[0]
    con.execute("checkpoint")
    con.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(SEED_WAREHOUSE))
    a = ap.parse_args()
    counts = build(Path(a.out), a.devices, a.seed)
    width = max(map(len, counts))
    for t, n in counts.items():
        print(f"  marts.{t:<{width}} {n:>7,} rows")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
