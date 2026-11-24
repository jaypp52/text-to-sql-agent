"""Source of eval/questions.jsonl (labelled question set).

    python -m eval.questions_src          # rewrite questions.jsonl from this file

Every item: id, question, gold_sql, difficulty (easy|medium|hard), tags, order_matters,
verified_by. Questions were drafted with Claude's help; every gold SQL was run against the
platform warehouse and its result recorded in eval/gold_results.json
(`make gold`). ``verified_by`` stays "pending" until Jay has checked the SQL and result by
hand with `make verify-questions`, which records "jay". Edit questions here, not in the
JSONL, so the order and formatting stay stable.

Window: the platform's backfill covers 2026-01-01..2026-01-07 (UTC). The warehouse also
holds a few minutes of live-demo data on 2026-11-23, so questions that should be limited
to January say so.
"""

from __future__ import annotations

import json
from pathlib import Path

W = "event_date BETWEEN DATE '2026-01-01' AND DATE '2026-01-07'"

Q: list[dict] = [
    # ---------------------------------------------------------------- easy (15)
    dict(id="e01", difficulty="easy", tags=["count", "dim_device"],
         question="How many devices are in the fleet?",
         gold_sql="SELECT count(DISTINCT device_id) FROM marts.dim_device"),
    dict(id="e02", difficulty="easy", tags=["count", "filter", "dim_device"],
         question="How many devices are installed in the us-west region?",
         gold_sql="SELECT count(DISTINCT device_id) FROM marts.dim_device WHERE region_id = 'us-west'"),
    dict(id="e03", difficulty="easy", tags=["group_by", "dim_device"],
         question="How many devices are there of each hardware model?",
         gold_sql="SELECT hardware_model, count(DISTINCT device_id) AS devices "
                  "FROM marts.dim_device GROUP BY hardware_model"),
    dict(id="e04", difficulty="easy", tags=["filter", "dim_firmware"],
         question="Which firmware version is the latest release, and when was it released?",
         gold_sql="SELECT firmware_version, release_date FROM marts.dim_firmware WHERE is_latest"),
    dict(id="e05", difficulty="easy", tags=["count", "filter", "fct_device_event"],
         question="How many camera crash (CAM_CRASH) events are in the warehouse in total?",
         gold_sql="SELECT count(*) FROM marts.fct_device_event WHERE event_code = 'CAM_CRASH'"),
    dict(id="e06", difficulty="easy", tags=["count", "filter", "fct_trip"],
         question="How many complete trips started on 2026-01-03?",
         gold_sql="SELECT count(*) FROM marts.fct_trip "
                  "WHERE trip_date = DATE '2026-01-03' AND is_complete"),
    dict(id="e07", difficulty="easy", tags=["aggregate", "fct_trip"],
         question="What is the average distance in km of complete trips?",
         gold_sql="SELECT avg(distance_km) FROM marts.fct_trip WHERE is_complete"),
    dict(id="e08", difficulty="easy", tags=["filter", "dim_event_code"],
         question="Which event codes have severity 'critical'?",
         gold_sql="SELECT event_code FROM marts.dim_event_code WHERE severity = 'critical'"),
    dict(id="e09", difficulty="easy", tags=["aggregate", "fct_device_minute"],
         question="What is the highest board temperature ever recorded, in degrees Celsius?",
         gold_sql="SELECT max(temp_c) FROM marts.fct_device_minute"),
    dict(id="e10", difficulty="easy", tags=["group_by", "filter", "fct_device_event"],
         question="How many device events of each event code happened on 2026-01-05?",
         gold_sql="SELECT event_code, count(*) AS events FROM marts.fct_device_event "
                  "WHERE event_date = DATE '2026-01-05' GROUP BY event_code"),
    dict(id="e11", difficulty="easy", tags=["lookup", "fct_device_daily"],
         question="How many minutes did device dev-000042 drive on 2026-01-04?",
         gold_sql="SELECT drive_minutes FROM marts.fct_device_daily "
                  "WHERE device_id = 'dev-000042' AND event_date = DATE '2026-01-04'"),
    dict(id="e12", difficulty="easy", tags=["lookup", "dim_region"],
         question="What is the timezone of the India West region?",
         gold_sql="SELECT timezone FROM marts.dim_region WHERE region_name = 'India West'"),
    dict(id="e13", difficulty="easy", tags=["count_distinct", "fct_device_event"],
         question="How many distinct devices reported at least one OVERHEAT event?",
         gold_sql="SELECT count(DISTINCT device_id) FROM marts.fct_device_event "
                  "WHERE event_code = 'OVERHEAT'"),
    dict(id="e14", difficulty="easy", tags=["count", "fct_trip"],
         question="How many trips are incomplete (the trip start or end was not loaded)?",
         gold_sql="SELECT count(*) FROM marts.fct_trip WHERE NOT is_complete"),
    dict(id="e15", difficulty="easy", tags=["count", "dim_device", "scd2"],
         question="How many devices are currently running firmware 1.1.0?",
         gold_sql="SELECT count(DISTINCT device_id) FROM marts.dim_device "
                  "WHERE is_current AND firmware_version = '1.1.0'"),
    # ---------------------------------------------------------------- medium (15)
    dict(id="m01", difficulty="medium", tags=["rate", "fct_firmware_daily", "date_window"],
         question="What was the crash rate per 100 device-days for each firmware version "
                  "between 2026-01-01 and 2026-01-07? Round to 2 decimals.",
         gold_sql="SELECT firmware_version, round(100.0 * sum(crash_count) / sum(device_days), 2) "
                  f"AS crash_rate FROM marts.fct_firmware_daily WHERE {W} "
                  "GROUP BY firmware_version"),
    dict(id="m02", difficulty="medium", tags=["rate", "fct_firmware_daily", "date_window"],
         question="Crash rate per 100 device-days by hardware model and firmware version for "
                  "2026-01-01 to 2026-01-07, rounded to 2 decimals.",
         gold_sql="SELECT hardware_model, firmware_version, "
                  "round(100.0 * sum(crash_count) / sum(device_days), 2) AS crash_rate "
                  f"FROM marts.fct_firmware_daily WHERE {W} "
                  "GROUP BY hardware_model, firmware_version"),
    dict(id="m03", difficulty="medium", tags=["metric_definition", "fct_device_daily", "date_window"],
         question="What was the average daily LTE loss percentage per region in the first "
                  "week of January 2026 (average of the device-day lte_loss_pct), rounded to 2 "
                  "decimals?",
         gold_sql="SELECT region_id, round(avg(lte_loss_pct), 2) AS avg_lte_loss_pct "
                  f"FROM marts.fct_device_daily WHERE {W} GROUP BY region_id"),
    dict(id="m04", difficulty="medium", tags=["top_n", "fct_trip", "date_window"],
         question="Which 5 devices drove the most total distance in complete trips between "
                  "2026-01-01 and 2026-01-07? Give the device and its total km.",
         order_matters=True,
         gold_sql="SELECT device_id, sum(distance_km) AS total_km FROM marts.fct_trip "
                  "WHERE is_complete AND trip_date BETWEEN DATE '2026-01-01' AND DATE '2026-01-07' "
                  "GROUP BY device_id ORDER BY total_km DESC LIMIT 5"),
    dict(id="m05", difficulty="medium", tags=["join", "dim_date", "fct_trip"],
         question="How many trips started on weekend days versus weekdays between 2026-01-01 "
                  "and 2026-01-07? Return is_weekend and the number of trips.",
         gold_sql="SELECT d.is_weekend, count(*) AS trips FROM marts.fct_trip t "
                  "JOIN marts.dim_date d ON t.trip_date = d.date_day "
                  "WHERE t.trip_date BETWEEN DATE '2026-01-01' AND DATE '2026-01-07' "
                  "GROUP BY d.is_weekend"),
    dict(id="m06", difficulty="medium", tags=["time_series", "fct_device_event", "date_window"],
         question="Daily number of CAM_CRASH events on TX-200 devices from 2026-01-01 to "
                  "2026-01-07, by date.",
         order_matters=True,
         gold_sql="SELECT event_date, count(*) AS crashes FROM marts.fct_device_event "
                  f"WHERE event_code = 'CAM_CRASH' AND hardware_model = 'TX-200' AND {W} "
                  "GROUP BY event_date ORDER BY event_date"),
    dict(id="m07", difficulty="medium", tags=["count_distinct", "group_by", "date_window"],
         question="For each region, how many distinct devices had at least one camera crash "
                  "between 2026-01-01 and 2026-01-07?",
         gold_sql="SELECT region_id, count(DISTINCT device_id) AS devices "
                  "FROM marts.fct_device_event "
                  f"WHERE event_code = 'CAM_CRASH' AND {W} GROUP BY region_id"),
    dict(id="m08", difficulty="medium", tags=["aggregate", "group_by", "fct_trip"],
         question="What is the average duration in minutes of complete trips for each "
                  "hardware model?",
         gold_sql="SELECT hardware_model, avg(duration_min) AS avg_duration_min "
                  "FROM marts.fct_trip WHERE is_complete GROUP BY hardware_model"),
    dict(id="m09", difficulty="medium", tags=["scd2", "dim_device", "top_n"],
         question="On which day did the most devices start running firmware 1.1.0, and how "
                  "many devices was that?",
         gold_sql="SELECT CAST(valid_from AS DATE) AS day, count(*) AS devices "
                  "FROM marts.dim_device WHERE firmware_version = '1.1.0' "
                  "GROUP BY day ORDER BY devices DESC LIMIT 1"),
    dict(id="m10", difficulty="medium", tags=["aggregate", "group_by", "filter"],
         question="Total drive hours per region on 2026-01-06.",
         gold_sql="SELECT region_id, sum(drive_minutes) / 60.0 AS drive_hours "
                  "FROM marts.fct_device_daily WHERE event_date = DATE '2026-01-06' "
                  "GROUP BY region_id"),
    dict(id="m11", difficulty="medium", tags=["aggregate", "top_n", "fct_device_minute"],
         question="Which hardware model has the highest average board temperature over all "
                  "drive minutes, and what is that average?",
         gold_sql="SELECT hardware_model, avg(temp_c) AS avg_temp_c FROM marts.fct_device_minute "
                  "GROUP BY hardware_model ORDER BY avg_temp_c DESC LIMIT 1"),
    dict(id="m12", difficulty="medium", tags=["metric_definition", "filter", "fct_device_daily"],
         question="How many device-days had an LTE loss above 20 percent?",
         gold_sql="SELECT count(*) FROM marts.fct_device_daily WHERE lte_loss_pct > 20"),
    dict(id="m13", difficulty="medium", tags=["metric_definition", "group_by", "date_window"],
         question="Average GPS valid percentage per hardware model between 2026-01-01 and "
                  "2026-01-07 (average of device-days), rounded to 2 decimals.",
         gold_sql="SELECT hardware_model, round(avg(gps_valid_pct), 2) AS avg_gps_valid_pct "
                  f"FROM marts.fct_device_daily WHERE {W} GROUP BY hardware_model"),
    dict(id="m14", difficulty="medium", tags=["group_by", "fct_device_event"],
         question="How many REBOOT events did each firmware version produce?",
         gold_sql="SELECT firmware_version, count(*) AS reboots FROM marts.fct_device_event "
                  "WHERE event_code = 'REBOOT' GROUP BY firmware_version"),
    dict(id="m15", difficulty="medium", tags=["join", "dim_event_code", "fct_device_event"],
         question="How many device events were there in each event category (camera, system, "
                  "thermal, ...) between 2026-01-01 and 2026-01-07?",
         gold_sql="SELECT c.category, count(*) AS events FROM marts.fct_device_event e "
                  "JOIN marts.dim_event_code c ON e.event_code = c.event_code "
                  "WHERE e.event_date BETWEEN DATE '2026-01-01' AND DATE '2026-01-07' "
                  "GROUP BY c.category"),
    # ---------------------------------------------------------------- hard (14)
    dict(id="h01", difficulty="hard", tags=["before_after", "firmware", "fct_device_daily"],
         question="Which hardware model's average LTE loss rose most after upgrading to "
                  "1.1.0? Compare the average device-day lte_loss_pct on 1.0.0 and on 1.1.0 "
                  "between 2026-01-01 and 2026-01-07 and give the model and the increase in "
                  "percentage points.",
         gold_sql="SELECT hardware_model, avg(lte_loss_pct) FILTER (WHERE firmware_version = '1.1.0') "
                  "- avg(lte_loss_pct) FILTER (WHERE firmware_version = '1.0.0') AS increase_pp "
                  f"FROM marts.fct_device_daily WHERE {W} "
                  "GROUP BY hardware_model ORDER BY increase_pp DESC LIMIT 1"),
    dict(id="h02", difficulty="hard", tags=["before_after", "rate", "fct_firmware_daily"],
         question="For TX-200 devices, compare the crash rate per 100 device-days on firmware "
                  "1.0.0 with the rate on 1.1.0 between 2026-01-01 and 2026-01-07, rounded to "
                  "2 decimals.",
         gold_sql="SELECT firmware_version, round(100.0 * sum(crash_count) / sum(device_days), 2) "
                  "AS crash_rate FROM marts.fct_firmware_daily "
                  f"WHERE hardware_model = 'TX-200' AND {W} GROUP BY firmware_version"),
    dict(id="h03", difficulty="hard", tags=["scd2", "as_of", "dim_device"],
         question="Which firmware version was device dev-000004 running at 2026-01-05 12:00 UTC?",
         gold_sql="SELECT firmware_version FROM marts.dim_device WHERE device_id = 'dev-000004' "
                  "AND valid_from <= TIMESTAMP '2026-01-05 12:00:00' "
                  "AND TIMESTAMP '2026-01-05 12:00:00' < valid_to"),
    dict(id="h04", difficulty="hard", tags=["scd2", "as_of", "dim_device"],
         question="How many devices were running firmware 1.1.0 at 2026-01-04 00:00 UTC?",
         gold_sql="SELECT count(DISTINCT device_id) FROM marts.dim_device "
                  "WHERE firmware_version = '1.1.0' "
                  "AND valid_from <= TIMESTAMP '2026-01-04 00:00:00' "
                  "AND TIMESTAMP '2026-01-04 00:00:00' < valid_to"),
    dict(id="h05", difficulty="hard", tags=["scd2", "as_of", "dim_date", "rollout"],
         question="What percentage of the fleet was running firmware 1.1.0 at 23:59:59 UTC on "
                  "each day from 2026-01-01 to 2026-01-07? Round to 2 decimals.",
         order_matters=True,
         gold_sql="SELECT d.date_day, round(100.0 * count(DISTINCT v.device_id) FILTER "
                  "(WHERE v.firmware_version = '1.1.0') / count(DISTINCT v.device_id), 2) AS pct_on_1_1_0 "
                  "FROM marts.dim_date d JOIN marts.dim_device v "
                  "ON v.valid_from <= d.date_day + INTERVAL '23:59:59' "
                  "AND d.date_day + INTERVAL '23:59:59' < v.valid_to "
                  "WHERE d.date_day BETWEEN DATE '2026-01-01' AND DATE '2026-01-07' "
                  "GROUP BY d.date_day ORDER BY d.date_day"),
    dict(id="h06", difficulty="hard", tags=["window", "top_per_group", "fct_device_daily"],
         question="For each day from 2026-01-01 to 2026-01-07, which device drove the most "
                  "minutes, and how many minutes was it?",
         order_matters=True,
         gold_sql="SELECT event_date, device_id, drive_minutes FROM marts.fct_device_daily "
                  f"WHERE {W} QUALIFY row_number() OVER "
                  "(PARTITION BY event_date ORDER BY drive_minutes DESC, device_id) = 1 "
                  "ORDER BY event_date"),
    dict(id="h07", difficulty="hard", tags=["percentile", "fct_trip"],
         question="What is the median distance of complete trips for each hardware model?",
         gold_sql="SELECT hardware_model, median(distance_km) AS median_km FROM marts.fct_trip "
                  "WHERE is_complete GROUP BY hardware_model"),
    dict(id="h08", difficulty="hard", tags=["percentile", "fct_device_minute"],
         question="What is the 95th percentile of board temperature for each hardware model "
                  "(continuous percentile over drive minutes)?",
         gold_sql="SELECT hardware_model, quantile_cont(temp_c, 0.95) AS p95_temp_c "
                  "FROM marts.fct_device_minute GROUP BY hardware_model"),
    dict(id="h09", difficulty="hard", tags=["window", "lag", "time_series"],
         question="Day-over-day change in the fleet's total number of camera crashes, for "
                  "each day from 2026-01-02 to 2026-01-07.",
         order_matters=True,
         gold_sql="WITH daily AS (SELECT event_date, count(*) AS crashes "
                  "FROM marts.fct_device_event WHERE event_code = 'CAM_CRASH' "
                  f"AND {W} GROUP BY event_date) "
                  "SELECT event_date, crashes - lag(crashes) OVER (ORDER BY event_date) AS change "
                  "FROM daily QUALIFY event_date >= DATE '2026-01-02' ORDER BY event_date"),
    dict(id="h10", difficulty="hard", tags=["before_after", "firmware", "fct_device_daily"],
         question="For devices that upgraded to 1.1.0 during 2026-01-01 to 2026-01-07, what "
                  "was their average daily LTE loss percentage on days fully on 1.0.0 versus "
                  "days fully on 1.1.0 (excluding upgrade days)? Round to 2 decimals.",
         gold_sql="SELECT firmware_version, round(avg(lte_loss_pct), 2) AS avg_lte_loss_pct "
                  f"FROM marts.fct_device_daily WHERE {W} AND NOT firmware_changed "
                  "AND device_id IN (SELECT device_id FROM marts.dim_device "
                  "WHERE firmware_version = '1.1.0' AND valid_from >= TIMESTAMP '2026-01-01' "
                  "AND valid_from < TIMESTAMP '2026-01-08') "
                  "GROUP BY firmware_version"),
    dict(id="h11", difficulty="hard", tags=["rate", "ranking", "fct_device_daily"],
         question="Rank the regions by camera-crash rate per 100 device-days between "
                  "2026-01-01 and 2026-01-07, highest first, rounded to 2 decimals.",
         order_matters=True,
         gold_sql="SELECT region_id, round(100.0 * sum(crash_count) / count(*), 2) AS crash_rate "
                  f"FROM marts.fct_device_daily WHERE {W} "
                  "GROUP BY region_id ORDER BY crash_rate DESC, region_id"),
    dict(id="h12", difficulty="hard", tags=["window", "running_total", "fct_trip"],
         question="Running total of complete trips by day from 2026-01-01 to 2026-01-07.",
         order_matters=True,
         gold_sql="SELECT trip_date, sum(count(*)) OVER (ORDER BY trip_date) AS running_trips "
                  "FROM marts.fct_trip WHERE is_complete "
                  "AND trip_date BETWEEN DATE '2026-01-01' AND DATE '2026-01-07' "
                  "GROUP BY trip_date ORDER BY trip_date"),
    dict(id="h13", difficulty="hard", tags=["having", "count_distinct", "fct_device_event"],
         question="Which devices had a camera crash on at least 3 different days between "
                  "2026-01-01 and 2026-01-07?",
         gold_sql="SELECT device_id FROM marts.fct_device_event "
                  f"WHERE event_code = 'CAM_CRASH' AND {W} GROUP BY device_id "
                  "HAVING count(DISTINCT event_date) >= 3"),
    dict(id="h14", difficulty="hard", tags=["self_join", "sequence", "fct_device_event"],
         question="What percentage of camera crashes were followed by a REBOOT of the same "
                  "device within 5 minutes? Round to 1 decimal.",
         gold_sql="SELECT round(100.0 * count(*) FILTER (WHERE EXISTS ("
                  "SELECT 1 FROM marts.fct_device_event r WHERE r.device_id = c.device_id "
                  "AND r.event_code = 'REBOOT' AND r.event_ts >= c.event_ts "
                  "AND r.event_ts <= c.event_ts + INTERVAL 5 MINUTE)) / count(*), 1) AS pct "
                  "FROM marts.fct_device_event c WHERE c.event_code = 'CAM_CRASH'"),
    # ---------------------------------------------------------------- unanswerable (6)
    dict(id="u01", difficulty="hard", tags=["unanswerable"],
         question="What is the name of the driver assigned to device dev-000010?", gold_sql=""),
    dict(id="u02", difficulty="medium", tags=["unanswerable"],
         question="What was the average fuel consumption per trip in liters?", gold_sql=""),
    dict(id="u03", difficulty="medium", tags=["unanswerable"],
         question="How many harsh-braking events did TX-300 devices record?", gold_sql=""),
    dict(id="u04", difficulty="easy", tags=["unanswerable"],
         question="What is the monthly subscription revenue per region?", gold_sql=""),
    dict(id="u05", difficulty="medium", tags=["unanswerable"],
         question="Was it raining during the trips in us-east on 2026-01-03?", gold_sql=""),
    dict(id="u06", difficulty="easy", tags=["unanswerable"],
         question="How many video clips did the cameras upload to the cloud each day?",
         gold_sql=""),
]

OUT = Path(__file__).with_name("questions.jsonl")


def items() -> list[dict]:
    verified = {}
    if OUT.exists():  # keep verification already recorded by make verify-questions
        for line in OUT.read_text().splitlines():
            if line.strip():
                q = json.loads(line)
                verified[q["id"]] = (q.get("verified_by"), q.get("gold_sql"))
    out = []
    for q in Q:
        item = {
            "id": q["id"],
            "question": q["question"],
            "gold_sql": q["gold_sql"],
            "difficulty": q["difficulty"],
            "tags": q["tags"],
            "order_matters": q.get("order_matters", False),
            "unanswerable": "unanswerable" in q["tags"],
            "verified_by": "pending",
        }
        who, sql = verified.get(q["id"], (None, None))
        if who and who != "pending" and sql == q["gold_sql"]:
            item["verified_by"] = who  # still valid: the SQL did not change
        out.append(item)
    return out


def main() -> None:
    rows = items()
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate ids"
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"wrote {OUT} ({len(rows)} questions)")


if __name__ == "__main__":
    main()
