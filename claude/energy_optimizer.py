# pyscript/energy_optimizer.py
"""
Energy Optimizer — pyscript (HACS)

Two-layer control:
  Strategic (15 min): InfluxDB history + Solcast + EPEX → operating MODE + guidance setpoint
  Tactical   (5 sec): real-time proportional controller targeting grid_power ≈ 0

Requires in configuration.yaml:
  pyscript:
    allow_all_imports: true

Only file needed:
  /config/pyscript/energy_optimizer.py
"""

import aiohttp
import pytz
from datetime import datetime, timedelta

# ── Hardware constants ────────────────────────────────────────────────────
BATTERY_SIZE_WH   = 2760
OUTPUT_MIN_W      = -1200
OUTPUT_MAX_W      =  1200
BATTERY_FULL_PCT  =  98
BATTERY_EMPTY_PCT =  15
GRID_DEADZONE_W   =  10

# ── Real-time proportional controller tuning ─────────────────────────────
RT_GAIN      = 0.8
RT_ROUND_W   = 10
RT_MIN_DELTA = 10

# ── InfluxDB ──────────────────────────────────────────────────────────────
INFLUX_URL    = "http://localhost:8086/query"
INFLUX_DB     = "homeassistant"
INFLUX_USER   = "homeassistant"
INFLUX_PASS   = "hainflux!"
INFLUX_ENTITY = "total_consumption"
INFLUX_UNIT   = "W"            # change to "kW" if sensor reports in kW

# ── HA entity IDs ─────────────────────────────────────────────────────────
E_GRID_POWER     = "sensor.shrdzm_485519e15aae_16_7_0"
E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER  = "sensor.ezhi_battery_power"
E_INV_OUTPUT     = "number.apsystems_ezhi_max_output_power"
E_PRICE_DATA     = "sensor.epex_spot_data_total_price"
E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

# ── Shared context between strategic and tactical layers ──────────────────
_ctx = {
    "mode":         "BALANCE",
    "setpoint":     0,
    "strategic_sp": 0,
    "price":        0.15,
    "p25":          0.10,
    "p75":          0.20,
    "last_soc":     50.0,
}

TZ = pytz.timezone("Europe/Vienna")


# ════════════════════════════════════════════════════════════════════════════
# INFLUXDB — native async via aiohttp
# ════════════════════════════════════════════════════════════════════════════

async def _influx_query(q: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            INFLUX_URL,
            params={"db": INFLUX_DB, "u": INFLUX_USER, "p": INFLUX_PASS, "q": q},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


async def _fetch_historical_consumption() -> dict:
    """
    Queries InfluxDB for the past 4 same-weekday full days.
    Returns {(hour, quarter_idx 0-3): mean_watts}.
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
            data   = await _influx_query(q)
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
            log.warning(f"InfluxDB query error (week -{week_back}): {exc}")

    if not accum:
        log.warning("No InfluxDB data — using fallback consumption profile")
        return _fallback_consumption()

    result = {}
    for k, v in accum.items():
        total = 0.0
        for x in v:
            total += x
        result[k] = total / len(v)
    return result


def _fallback_consumption() -> dict:
    hourly = [150, 150, 150, 150, 150, 150,   # 00-06 night
              600, 600, 600,                   # 06-09 morning
              350, 350, 350, 350, 350,         # 09-14 daytime
              350, 350, 350, 350,              # 14-17 daytime
              700, 700, 700, 700, 700,         # 17-22 evening
              300, 300]                        # 22-24 late
    result = {}
    for h in range(24):
        for q in range(4):
            result[(h, q)] = hourly[h]
    return result


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_solar_forecast() -> dict:
    solar = {}
    try:
        attrs = state.getattr(E_SOLAR_HOUR) or {}
        fl = attrs.get("forecast") or attrs.get("detailedForecast") or \
             attrs.get("DetailedForecast") or attrs.get("forecasts") or []
        for entry in fl:
            t_str = entry.get("period_start") or entry.get("PeriodStart")
            pv_kw = float(entry.get("pv_estimate") or entry.get("PvEstimate") or 0)
            if t_str:
                t = datetime.fromisoformat(t_str).astimezone(TZ)
                solar[t.hour] = solar.get(t.hour, 0.0) + pv_kw * 1000
        if not solar:
            val = float(state.get(E_SOLAR_HOUR) or 0)
            solar[datetime.now(TZ).hour] = val
    except Exception as exc:
        log.warning(f"Solar forecast error: {exc}")
    return solar


def _get_spot_prices() -> dict:
    prices = {}
    try:
        data = (state.getattr(E_PRICE_DATA) or {}).get("data", [])
        log.info(f"EPEX data slots found: {len(data)}")
        for entry in data:
            t = datetime.fromisoformat(entry["start_time"]).astimezone(TZ)
            prices[(t.hour, t.minute // 15)] = float(entry["price_per_kwh"])
    except Exception as exc:
        log.error(f"Spot price error: {exc}")
    return prices


def _build_schedule(consumption: dict, solar: dict, prices: dict) -> list:
    now = datetime.now(TZ)
    out = []
    for i in range(96):
        t   = now + timedelta(minutes=15 * i)
        key = (t.hour, t.minute // 15)
        c   = consumption.get(key, 300.0)
        s   = solar.get(t.hour, 0.0) / 4.0
        p   = prices.get(key, 0.15)
        out.append({
            "i":     i,
            "time":  t,
            "cons":  c,
            "solar": s,
            "price": p,
            "net":   c - s,
        })
    return out


def _compute_mode(soc: float, schedule: list) -> tuple:
    try:
        if not schedule:
            return ("BALANCE", 0, 0.10, 0.20)

        prices_list = []
        for s in schedule:
            prices_list.append(s["price"])
        prices_sorted = sorted(prices_list)

        n   = len(prices_sorted)
        p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
        p75 = prices_sorted[min(n - 1, int(n * 0.75))]

        price = schedule[0]["price"]
        net   = schedule[0]["net"]

        log.info(f"_compute_mode: SOC={soc} price={price} p25={p25} p75={p75} net={net}")

        if soc <= BATTERY_EMPTY_PCT:
            if price <= p25:
                return ("GRID_CHARGE", OUTPUT_MIN_W, p25, p75)
            else:
                return ("BALANCE", 0, p25, p75)
        elif soc >= BATTERY_FULL_PCT:
            if price > 0:
                return ("EXPORT", OUTPUT_MAX_W, p25, p75)
            else:
                return ("BALANCE", max(0, int(net)), p25, p75)
        elif price >= p75:
            return ("DISCHARGE", OUTPUT_MAX_W, p25, p75)
        elif price <= p25:
            return ("GRID_CHARGE", OUTPUT_MIN_W, p25, p75)
        else:
            return ("BALANCE", int(net), p25, p75)

    except Exception as exc:
        log.error(f"_compute_mode error: {exc}")
        return ("BALANCE", 0, 0.10, 0.20)


def _apply_if_changed(new_sp: int):
    new_sp  = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, new_sp))
    current = _ctx["setpoint"]
    if abs(new_sp - current) >= RT_MIN_DELTA:
        number.set_value(entity_id=E_INV_OUTPUT, value=new_sp)
        _ctx["setpoint"] = new_sp
        log.debug(f"⚡ inverter {current:+d} W → {new_sp:+d} W")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGIC LAYER — every 15 minutes
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("period(now, 15min)")
async def strategic_optimize():
    log.info("── Strategic optimization cycle ──")
    try:
        soc_raw = state.get(E_BATTERY_SOC)
        if soc_raw in (None, "unavailable", "unknown"):
            log.warning("Battery SOC unavailable — skipping strategic cycle")
            return
        soc = float(soc_raw)
        _ctx["last_soc"] = soc

        consumption = await _fetch_historical_consumption()
        solar       = _get_solar_forecast()
        prices      = _get_spot_prices()

        if not prices:
            log.warning("No EPEX price data available — mode unchanged")
            return

        schedule           = _build_schedule(consumption, solar, prices)
        mode, sp, p25, p75 = _compute_mode(soc, schedule)

        _ctx["mode"]         = mode
        _ctx["strategic_sp"] = sp
        _ctx["p25"]          = p25
        _ctx["p75"]          = p75
        _ctx["price"]        = schedule[0]["price"] if schedule else 0.15

        if mode != "BALANCE":
            _apply_if_changed(sp)

        log.info(
            f"Mode={mode} | SOC={soc:.0f}% | "
            f"Price={_ctx['price']:.4f} €/kWh "
            f"[P25={p25:.4f} P75={p75:.4f}] | "
            f"Guidance={sp:+d} W"
        )

    except Exception as exc:
        import traceback
        log.error(f"Strategic error: {exc}\n{traceback.format_exc()}")


# ════════════════════════════════════════════════════════════════════════════
# TACTICAL LAYER — every 5 seconds
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("period(now, 5s)")
def realtime_control():
    try:
        mode = _ctx["mode"]

        if mode != "BALANCE":
            _apply_if_changed(_ctx["strategic_sp"])
            return

        grid_raw = state.get(E_GRID_POWER)
        if grid_raw in (None, "unavailable", "unknown"):
            return
        grid = float(grid_raw)

        soc_raw = state.get(E_BATTERY_SOC)
        soc     = float(soc_raw) if soc_raw not in (None, "unavailable", "unknown") \
                  else _ctx["last_soc"]
        _ctx["last_soc"] = soc

        if abs(grid) <= GRID_DEADZONE_W:
            return

        current_sp = _ctx["setpoint"]
        raw_sp     = current_sp + RT_GAIN * grid
        new_sp     = int(round(raw_sp / RT_ROUND_W) * RT_ROUND_W)

        if new_sp > current_sp and soc <= BATTERY_EMPTY_PCT:
            return
        if new_sp < current_sp and soc >= BATTERY_FULL_PCT:
            return

        _apply_if_changed(new_sp)

    except Exception as exc:
        log.error(f"Realtime control error: {exc}")


# ════════════════════════════════════════════════════════════════════════════
# EVENT TRIGGERS
# ════════════════════════════════════════════════════════════════════════════

@state_trigger(E_PRICE_DATA)
async def on_price_update(**kwargs):
    log.info("EPEX price data updated — triggering strategic cycle")
    await strategic_optimize()


@state_trigger(E_BATTERY_SOC)
def on_soc_critical(**kwargs):
    soc_raw = state.get(E_BATTERY_SOC)
    if soc_raw in (None, "unavailable", "unknown"):
        return
    if float(soc_raw) < 12:
        number.set_value(entity_id=E_INV_OUTPUT, value=0)
        _ctx["mode"]         = "BALANCE"
        _ctx["setpoint"]     = 0
        _ctx["strategic_sp"] = 0
        persistent_notification.create(
            title="⚠️ Battery Critical",
            message=f"SOC is {soc_raw}% — inverter forced to 0 W. "
                    f"Optimizer resumes at next 15-min strategic cycle.",
            notification_id="energy_optimizer_critical",
        )
        log.warning(f"Battery critical ({soc_raw}%) — inverter forced to 0 W")


# ════════════════════════════════════════════════════════════════════════════
# MANUAL SERVICE CALL
# ════════════════════════════════════════════════════════════════════════════

@service
async def energy_optimizer_force_run():
    """Callable via Developer Tools → Actions → pyscript.energy_optimizer_force_run"""
    log.info("Manual trigger — running strategic cycle now")
    await strategic_optimize()
