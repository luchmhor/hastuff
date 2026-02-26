# pyscript/modules/influx_helper.py
# Plain Python module — no pyscript decorators or globals.
# Importable by pyscript with allow_all_imports: true

import requests
import statistics
import pytz
from datetime import datetime, timedelta

TZ = pytz.timezone("Europe/Vienna")

INFLUX_URL    = "http://localhost:8086/query"
INFLUX_DB     = "homeassistant"
INFLUX_USER   = "homeassistant"
INFLUX_PASS   = "hainflux!"
INFLUX_ENTITY = "total_consumption"
INFLUX_UNIT   = "W"


def _influx_query(q: str) -> dict:
    resp = requests.get(
        INFLUX_URL,
        params={"db": INFLUX_DB, "u": INFLUX_USER, "p": INFLUX_PASS, "q": q},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_historical_consumption() -> dict:
    """
    Queries InfluxDB for the past 4 same-weekday full days.
    Returns {(hour, quarter_idx 0-3): mean_watts}.
    This is a plain Python function — safe to call from task.executor().
    """
    now   = datetime.now(TZ)
    accum = {}

    for week_back in range(1, 5):
        anchor    = now - timedelta(weeks=week_back)
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        s_utc     = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        e_utc     = day_end.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        q = (
            f'SELECT mean("value") FROM "{INFLUX_UNIT}" '
            f"WHERE \"entity_id\" = '{INFLUX_ENTITY}' "
            f"AND time >= '{s_utc}' AND time < '{e_utc}' "
            f"GROUP BY time(15m) fill(previous)"
        )
        try:
            data   = _influx_query(q)
            series = data.get("results", [{}])[0].get("series", [])
            if not series:
                continue
            cols     = series[0]["columns"]
            t_idx    = cols.index("time")
            mean_idx = cols.index("mean")
            for row in series[0].get("values", []):
                if row[mean_idx] is None:
                    continue
                t_local = datetime.fromisoformat(
                    row[t_idx].replace("Z", "+00:00")
                ).astimezone(TZ)
                key = (t_local.hour, t_local.minute // 15)
                accum.setdefault(key, []).append(row[mean_idx])
        except Exception as exc:
            # Return what we have so far; caller handles empty result
            print(f"InfluxDB query error (week -{week_back}): {exc}")

    if not accum:
        return _fallback_consumption()

    return {k: statistics.mean(v) for k, v in accum.items()}


def _fallback_consumption() -> dict:
    hourly = [150]*6 + [600]*3 + [350]*8 + [700]*5 + [300]*2
    return {(h, q): hourly[h] for h in range(24) for q in range(4)}
