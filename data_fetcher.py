# data_fetcher.py
"""
All external data acquisition – InfluxDB, Solcast (via HA state), EPEX prices.
Configuration values are read from CONFIG (loaded via _config).
"""

import aiohttp
import pytz
from datetime import datetime, timedelta, timezone
from ._config import CONFIG

# ---- helpers ---------------------------------------------------------
async def _influx_query(query: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            CONFIG.influx.url,
            params={
                "db": CONFIG.influx.database,
                "u": CONFIG.influx.username,
                "p": CONFIG.influx.password,
                "q": query,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


# ---- public functions ------------------------------------------------
async def fetch_historical_consumption() -> dict:
    """Return {(hour, quarter): mean_power_W} for the last 4 weeks."""
    now = datetime.now(pytz.timezone(CONFIG.general.timezone))
    accum = {}
    for week_back in range(1, 5):
        anchor = now - timedelta(weeks=week_back)
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        s_utc = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        e_utc = day_end.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = (
            f'SELECT mean("value") FROM "{CONFIG.influx.unit}" '
            f"WHERE \"entity_id\" = '{CONFIG.influx.entity_consumption}' "
            f"AND time >= '{s_utc}' AND time < '{e_utc}' "
            f"GROUP BY time(15m) fill(previous)"
        )
        try:
            data = await _influx_query(q)
            series = data.get("results", [{}])[0].get("series", [])
            if not series:
                continue
            cols = series[0]["columns"]
            t_idx = cols.index("time")
            m_idx = cols.index("mean")
            for row in series[0].get("values", []):
                if row[m_idx] is None:
                    continue
                t_local = datetime.fromisoformat(
                    row[t_idx].replace("Z", "+00:00")
                ).astimezone(pytz.timezone(CONFIG.general.timezone))
                key = (t_local.hour, t_local.minute // 15)
                accum.setdefault(key, []).append(row[m_idx])
        except Exception:
            continue  # skip problematic week

    if not accum:
        # ---- simple fallback (could also be moved to yaml if desired) ----
        fallback = {}
        hourly = [150]*6 + [600]*3 + [350]*7 + [350]*4 + [700]*5 + [300]*2
        for h in range(24):
            for q in range(4):
                fallback[(h, q)] = hourly[h]
        return fallback

    return {k: sum(v)/len(v) for k, v in accum.items()}


async def fetch_solar_actuals() -> dict:
    """Return {hour: actual_W} for hours already elapsed today."""
    now = datetime.now(pytz.timezone(CONFIG.general.timezone))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s_utc = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e_utc = now.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        f'SELECT mean("value") FROM "{CONFIG.influx.unit}" '
        f"WHERE \"entity_id\" = '{CONFIG.influx.entity_pv}' "
        f"AND time >= '{s_utc}' AND time < '{e_utc}' "
        f"GROUP BY time(1h) fill(previous)"
    )
    try:
        data = await _influx_query(q)
        series = data.get("results", [{}])[0].get("series", [])
        if not series:
            return {}
        cols = series[0]["columns"]
        t_idx = cols.index("time")
        m_idx = cols.index("mean")
        actuals = {}
        for row in series[0].get("values", []):
            if row[m_idx] is None:
                continue
            t_local = datetime.fromisoformat(
                row[t_idx].replace("Z", "+00:00")
            ).astimezone(pytz.timezone(CONFIG.general.timezone))
            actuals[t_local.hour] = float(row[m_idx])
        return actuals
    except Exception:
        return {}


def blend_solar_forecast(actuals: dict, state_getter) -> dict:
    """
    Combine actuals with Solcast forecast.
    `state_getter` is a callable that mimics `state.getattr` for the Solcast entities.
    """
    def _parse_hourly(fl, filter_date=None):
        out = {}
        for entry in fl:
            t_raw = entry.get("period_start")
            pv_kw = float(entry.get("pv_estimate") or 0)
            if t_raw is None:
                continue
            if isinstance(t_raw, str):
                t = datetime.fromisoformat(t_raw)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                t = t.astimezone(pytz.timezone(CONFIG.general.timezone))
            else:
                try:
                    t = t_raw.astimezone(pytz.timezone(CONFIG.general.timezone))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc).astimezone(pytz.timezone(CONFIG.general.timezone))
                except Exception:
                    t = datetime(*t_raw.timetuple()[:6], tzinfo=timezone.utc).astimezone(pytz.timezone(CONFIG.general.timezone))
            if filter_date is not None and t.date() != filter_date:
                continue
            out[t.hour] = pv_kw * 1000
        return out

    forecast = {}
    now = datetime.now(pytz.timezone(CONFIG.general.timezone))

    # today
    try:
        attrs = state_getter(CONFIG.solcast.today) or {}
        fl = attrs.get("detailedHourly") or []
        forecast.update(_parse_hourly(fl, filter_date=now.date()))
    except Exception:
        pass

    # tomorrow
    try:
        tomorrow = (now + timedelta(days=1)).date()
        attrs = state_getter(CONFIG.solcast.tomorrow) or {}
        fl = attrs.get("detailedHourly") or []
        forecast.update(_parse_hourly(fl, filter_date=tomorrow))
    except Exception:
        pass

    # ---- scale factor from last 2 completed hours ----
    scale = 1.0
    comp_hours = [
        actuals[h] / forecast[h]
        for h in range(max(0, now.hour - 2), now.hour)
        if h in actuals and h in forecast and forecast[h] > 50
    ]
    if comp_hours:
        scale = sum(comp_hours) / len(comp_hours)
        scale = max(0.3, min(2.0, scale))

    # ---- blended profile for next 24h ----
    solar = {}
    for h in range(now.hour, now.hour + 24):
        hod = h % 24
        hours_ahead = h - now.hour
        if hours_ahead < 0:                         # past
            solar[hod] = actuals.get(hod, forecast.get(hod, 0.0))
        elif hours_ahead == 0:                       # now – blend actual/forecast
            fc = forecast.get(hod, 0.0) * scale
            if hod in actuals:
                solar[hod] = 0.5 * actuals[hod] + 0.5 * fc
            else:
                solar[hod] = fc
        elif hours_ahead <= 2:                       # blend window
            blend = hours_ahead / 2.0
            scaled_fc = forecast.get(hod, 0.0) * scale
            pure_fc   = forecast.get(hod, 0.0)
            solar[hod] = (1 - blend) * scaled_fc + blend * pure_fc
        else:                                        # pure forecast
            solar[hod] = forecast.get(hod, 0.0)
    return solar


async def fetch_spot_prices() -> dict:
    """Return {(hour, quarter): price_€/kWh} (includes network fee)."""
    prices = {}
    try:
        raw_state = state.get(CONFIG.influx.price_sensor)        # `state` available in pyscript globals
        raw_attrs = state.getattr(CONFIG.influx.price_sensor) or {}
        data = raw_attrs.get("data", [])
        for entry in data:
            t = datetime.fromisoformat(entry["start_time"]).astimezone(pytz.timezone(CONFIG.general.timezone))
            epex = float(entry["price_per_kwh"])
            prices[(t.hour, t.minute // 15)] = epex + CONFIG.grid.network_fee_ct_per_kwh / 100.0
    except Exception:
        pass

    if not prices:   # fallback curve from yaml
        fallback_ct = CONFIG.pricing.fallback_hourly_ct
        prices = {
            (h, q): (ct + CONFIG.grid.network_fee_ct_per_kwh) / 100.0
            for h, ct in fallback_ct.items()
            for q in range(4)
        }
    return prices
