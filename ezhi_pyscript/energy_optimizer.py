# pyscript/energy_optimizer.py
"""
Energy Optimizer — pyscript (HACS) — Strategic planning layer only.

Runs every 15 minutes (and on EPEX price update events).
Writes mode and setpoint to input_number helpers for the HA tactical automation.

Requires in configuration.yaml:
pyscript:
  allow_all_imports: true

input_number:
  energy_optimizer_setpoint:
    name: Energy Optimizer Setpoint
    min: -1200
    max: 1200
    step: 10
    unit_of_measurement: W
    icon: mdi:lightning-bolt
  energy_optimizer_mode_id:
    name: Energy Optimizer Mode ID
    min: 0
    max: 3
    step: 1
    icon: mdi:state-machine
    # 0=BALANCE  1=GRID_CHARGE  2=DISCHARGE  3=TRICKLE

input_text:
  energy_optimizer_mode:
    name: Energy Optimizer Mode
    max: 32
    icon: mdi:battery-charging
  energy_optimizer_reason:
    name: Energy Optimizer Reason
    max: 255
    icon: mdi:information-outline

command_line:
  - sensor:
      name: energy_optimizer_outlook
      command: "python3 -c \"import json; f=open('/config/www/energy_outlook.md'); print(json.dumps({'content': f.read()}))\""
      scan_interval: 1800
      value_template: "OK"
      json_attributes:
        - content

Only file needed:
  /config/pyscript/energy_optimizer.py
"""

import aiohttp
import pytz
from datetime import datetime, timedelta, timezone
from scipy.optimize import linprog

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

USE_LP_OPTIMIZER = True

BATTERY_SIZE_WH     = 2760
OUTPUT_MIN_W        = -1200
OUTPUT_MAX_W        =  1200
BATTERY_FULL_PCT    =  98
BATTERY_TRICKLE_PCT =  96
BATTERY_EMPTY_PCT   =  15
GRID_DEADZONE_W     =  10
BATTERY_TRICKLE_W   =  10

BATTERY_CHARGE_EFF    = 0.95   # energy stored per Wh drawn from grid/PV
BATTERY_DISCHARGE_EFF = 0.95   # energy delivered per Wh taken from battery
# Round-trip = 0.95 × 0.95 = 0.9025  → ~10 % total system loss

NETWORK_FEE_CT_PER_KWH = 10.5
SCHEDULE_SLOTS          = 96   # 15-min slots per planning horizon (96 = 24 h)

DISCHARGE_PENALTY       = 0.0001
OPPORTUNITY_COST_WEIGHT = 0.5  # base weight for look-ahead term

ALLOW_EXPORT = False

PV_NAMEPLATE_WP = 1200   # nameplate peak power of PV array in Wp

# SOC threshold above which grid-charging is blocked.
# PV will fill remaining headroom for free → grid-charging wastes round-trip losses.
GRID_CHARGE_SOC_BLOCK_PCT  = 70   # hard block above this SOC
GRID_CHARGE_SOC_CHEAP_PCT  = 50   # between 50–70% only allow at p25 price or cheaper

INFLUX_URL        = "http://localhost:8086/query"
INFLUX_DB         = "homeassistant"
INFLUX_USER       = "homeassistant"
INFLUX_PASS       = "hainflux!"
INFLUX_ENTITY     = "total_consumption"
INFLUX_ENTITY_PV  = "ezhi_photovoltaic_power"
INFLUX_UNIT       = "W"

SOLAR_BLEND_HOURS = 2   # hours ahead over which actuals scale-factor is blended

E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER  = "sensor.ezhi_battery_power"
E_PRICE_DATA     = "sensor.epex_spot_data_total_price"
E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TODAY    = "sensor.solcast_pv_forecast_forecast_today"
E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

E_MODE_ID  = "input_number.energy_optimizer_mode_id"
E_SETPOINT = "input_number.energy_optimizer_setpoint"

MODE_IDS = {
    "BALANCE":     0,
    "GRID_CHARGE": 1,
    "DISCHARGE":   2,
    "TRICKLE":     3,
}

E_STATUS_MODE   = "input_text.energy_optimizer_mode"
E_STATUS_REASON = "input_text.energy_optimizer_reason"

OUTLOOK_FILE      = "/config/www/energy_outlook.md"
FORECAST_CSV_FILE = "/config/www/energy_forecast.csv"

LOG_DEBUG = True

TZ = pytz.timezone("Europe/Vienna")

_ctx = {
    "p25": 0.10,
    "p75": 0.20,
    "last_schedule": [],
}

# ════════════════════════════════════════════════════════════════════════════
# INFLUXDB HELPERS
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
    now   = datetime.now(TZ)
    accum = {}
    for week_back in range(1, 5):
        anchor    = now - timedelta(weeks=week_back)
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        s_utc = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        e_utc = day_end.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    # High-side estimate: 75th percentile per 15-min slot
    result = {}
    for k, values in accum.items():
        if not values:
            continue
        sorted_vals = sorted(values)
        idx = max(0, min(len(sorted_vals) - 1, int(0.75 * (len(sorted_vals) - 1))))
        result[k] = sorted_vals[idx]
    return result


def _fallback_consumption() -> dict:
    hourly = [
        150, 150, 150, 150, 150, 150,
        600, 600, 600,
        350, 350, 350, 350, 350,
        350, 350, 350, 350,
        700, 700, 700, 700, 700,
        300, 300,
    ]
    return {(h, q): hourly[h] for h in range(24) for q in range(4)}

# ════════════════════════════════════════════════════════════════════════════
# PV ACTUALS (InfluxDB)
# ════════════════════════════════════════════════════════════════════════════

async def _get_solar_actuals() -> dict:
    """Return {hour: watts} for hours that have already passed today."""
    now       = datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s_utc = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e_utc = now.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        f'SELECT mean("value") FROM "{INFLUX_UNIT}" '
        f"WHERE \"entity_id\" = '{INFLUX_ENTITY_PV}' "
        f"AND time >= '{s_utc}' AND time < '{e_utc}' "
        f"GROUP BY time(1h) fill(previous)"
    )
    actuals = {}
    try:
        data   = await _influx_query(q)
        series = data.get("results", [{}])[0].get("series", [])
        if not series:
            log.warning("No actual PV data from InfluxDB")
            return actuals
        cols     = series[0]["columns"]
        t_idx    = cols.index("time")
        mean_idx = cols.index("mean")
        for row in series[0].get("values", []):
            if row[mean_idx] is None:
                continue
            t_local = datetime.fromisoformat(
                row[t_idx].replace("Z", "+00:00")
            ).astimezone(TZ)
            actuals[t_local.hour] = float(row[mean_idx])
        log.info(f"PV actuals loaded: {len(actuals)} hours")
        if LOG_DEBUG:
            for h in sorted(actuals):
                log.info(f"  pv actual {h:02d}:00 = {actuals[h]:.0f}W")
    except Exception as exc:
        log.warning(f"PV actuals fetch error: {exc}")
    return actuals

# ════════════════════════════════════════════════════════════════════════════
# SOLAR FORECAST (Solcast + actuals blend)
# ════════════════════════════════════════════════════════════════════════════

def _get_solar_forecast(actuals: dict) -> dict:
    """
    Build per-hour solar estimate blending actuals with Solcast forecast.
    Past hours   : 100 % actual
    Current hour : 50 % actual + 50 % scaled forecast
    Next SOLAR_BLEND_HOURS : gradually shift from scaled → pure forecast
    Beyond blend window    : pure Solcast forecast
    """
    solar = {}
    now   = datetime.now(TZ)

    def _parse_hourly(fl, filter_date=None):
        result = {}
        for entry in fl:
            t_raw  = entry.get("period_start")
            pv_kw  = float(entry.get("pv_estimate") or 0)
            if t_raw is None:
                continue
            if isinstance(t_raw, str):
                t = datetime.fromisoformat(t_raw)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                t = t.astimezone(TZ)
            else:
                try:
                    t = t_raw.astimezone(TZ)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc).astimezone(TZ)
                except Exception:
                    t = datetime(*t_raw.timetuple()[:6], tzinfo=timezone.utc).astimezone(TZ)
            if filter_date is not None and t.date() != filter_date:
                continue
            result[t.hour] = pv_kw * 1000
        return result

    forecast = {}
    try:
        attrs = state.getattr(E_SOLAR_TODAY) or {}
        fl    = attrs.get("detailedHourly") or []
        for h, w in _parse_hourly(fl, filter_date=now.date()).items():
            forecast[h] = w
        log.info(f"Solcast today: {len(forecast)} hours loaded")
    except Exception as exc:
        log.warning(f"Solar today error: {exc}")

    try:
        tomorrow = (now + timedelta(days=1)).date()
        attrs    = state.getattr(E_SOLAR_TOMORROW) or {}
        fl       = attrs.get("detailedHourly") or []
        parsed   = _parse_hourly(fl, filter_date=tomorrow)
        for h, w in parsed.items():
            forecast[h] = w
        log.info(f"Solcast tomorrow: {len(parsed)} hours filled")
    except Exception as exc:
        log.warning(f"Solar tomorrow error: {exc}")

    # Actuals-based scale factor from last 2 completed hours
    scale = 1.0
    comparison_hours = [
        actuals[h] / forecast[h]
        for h in range(max(0, now.hour - 2), now.hour)
        if h in actuals and h in forecast and forecast[h] > 50
    ]
    if comparison_hours:
        scale = sum(comparison_hours) / len(comparison_hours)
        scale = max(0.3, min(2.0, scale))
        log.info(f"PV scale factor: {scale:.2f} (from {len(comparison_hours)} comparison hours)")
    else:
        log.info("PV scale factor: no comparison hours available, using 1.0")

    if forecast:
        for h in range(now.hour + 24):
            hour_of_day = h % 24
            hours_ahead = h - now.hour
            if hours_ahead < 0:
                solar[hour_of_day] = actuals.get(hour_of_day, forecast.get(hour_of_day, 0.0))
            elif hours_ahead == 0:
                fc = forecast.get(hour_of_day, 0.0) * scale
                if hour_of_day in actuals:
                    solar[hour_of_day] = 0.5 * actuals[hour_of_day] + 0.5 * fc
                else:
                    solar[hour_of_day] = fc
            elif hours_ahead <= SOLAR_BLEND_HOURS:
                blend_weight    = hours_ahead / SOLAR_BLEND_HOURS
                scaled_fc       = forecast.get(hour_of_day, 0.0) * scale
                pure_fc         = forecast.get(hour_of_day, 0.0)
                solar[hour_of_day] = (1 - blend_weight) * scaled_fc + blend_weight * pure_fc
            else:
                solar[hour_of_day] = forecast.get(hour_of_day, 0.0)

        if solar:
            peak_hour = max(solar, key=solar.get)
            log.info(f"Solar blended: {len(solar)} hours, peak {solar[peak_hour]:.0f}W at {peak_hour:02d}:00")
            if LOG_DEBUG:
                for h in sorted(solar):
                    if solar[h] > 0:
                        actual_str = f" | actual: {actuals[h]:.0f}W" if h in actuals else ""
                        fc_str     = f" | forecast: {forecast.get(h, 0):.0f}W"
                        log.info(f"  solar {h:02d}:00 = {solar[h]:.0f}W{actual_str}{fc_str}")
        return solar

    # Scalar fallback (no Solcast data at all)
    try:
        val = float(state.get(E_SOLAR_HOUR) or 0)
        solar[now.hour] = val
        log.warning(f"Solcast fallback: {val}W for current hour only")
    except Exception as exc:
        log.warning(f"Solar scalar fallback error: {exc}")
    return solar

# ════════════════════════════════════════════════════════════════════════════
# SPOT PRICES + SCHEDULE
# ════════════════════════════════════════════════════════════════════════════

def _fallback_prices() -> dict:
    hourly_ct = {
        0: 8.0,  1: 7.5,  2: 7.0,  3: 6.5,  4: 6.5,  5: 7.0,
        6: 18.0, 7: 22.0, 8: 20.0, 9: 15.0, 10: 11.0, 11: 8.0,
        12: 6.0, 13: 6.0, 14: 7.0, 15: 9.0,  16: 14.0, 17: 22.0,
        18: 26.0,19: 28.0,20: 24.0,21: 18.0, 22: 13.0, 23: 10.0,
    }
    return {
        (h, q): (ct + NETWORK_FEE_CT_PER_KWH) / 100.0
        for h, ct in hourly_ct.items()
        for q in range(4)
    }

def _get_spot_prices() -> dict:
    prices = {}
    try:
        raw_state = state.get(E_PRICE_DATA)
        raw_attrs = state.getattr(E_PRICE_DATA) or {}
        log.info(f"EPEX sensor state: {raw_state}")
        log.info(f"EPEX sensor attrs keys: {list(raw_attrs.keys())}")
        data = raw_attrs.get("data", [])
        log.info(f"EPEX data slots found: {len(data)}")
        if data:
            log.info(f"EPEX first entry: {data[0]}")
        for entry in data:
            t    = datetime.fromisoformat(entry["start_time"]).astimezone(TZ)
            epex = float(entry["price_per_kwh"])
            prices[(t.hour, t.minute // 15)] = epex + NETWORK_FEE_CT_PER_KWH / 100.0
    except Exception as exc:
        log.error(f"Spot price error: {exc}")

    if not prices:
        log.warning("EPEX data unavailable — using fallback price curve")
        return _fallback_prices()
    return prices


def _build_schedule(consumption: dict, solar: dict, prices: dict) -> list:
    now    = datetime.now(TZ)
    minute = (now.minute // 15) * 15
    now    = now.replace(minute=minute, second=0, microsecond=0)
    out    = []
    for i in range(SCHEDULE_SLOTS):
        t   = now + timedelta(minutes=15 * i)
        key = (t.hour, t.minute // 15)
        c   = consumption.get(key, 300.0)
        s   = solar.get(t.hour, 0.0)
        p   = prices.get(key, 0.15)
        out.append({"i": i, "time": t, "cons": c, "solar": s, "price": p, "net": c - s})
    return out

# ════════════════════════════════════════════════════════════════════════════
# LP OPTIMIZER
# ════════════════════════════════════════════════════════════════════════════

def _solve_optimal_schedule(soc: float, schedule: list) -> list:
    N  = len(schedule)
    DT = 0.25  # hours per slot

    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH

    loads  = [s["cons"]  for s in schedule]
    solars = [s["solar"] for s in schedule]
    prices = [s["price"] for s in schedule]

    pv_threshold = PV_NAMEPLATE_WP / 10.0
    surplus = [
        min(max(0.0, solars[t] - loads[t]), abs(OUTPUT_MIN_W))
        for t in range(N)
    ]

    price_max = max(prices)
    price_min = min(prices)
    price_avg = sum(prices) / N

    # Peak-sensitivity: how concentrated is price × load?
    cost_intensity  = [prices[t] * max(0.0, loads[t]) for t in range(N)]
    total_intensity = sum(cost_intensity) or 1.0
    sorted_intensity = sorted(cost_intensity, reverse=True)
    top_k = max(1, int(0.20 * N))
    frac_hotspots = sum(sorted_intensity[:top_k]) / total_intensity
    oc_scale = 0.5 + frac_hotspots      # ~0.5–1.5
    effective_oc_weight = OPPORTUNITY_COST_WEIGHT * oc_scale

    # Price quantile: lowest 20% for "no discharge" rule
    prices_sorted = sorted(prices)
    p20 = prices_sorted[max(0, int(0.20 * N) - 1)]

    # ── PV surplus look-ahead ────────────────────────────────────────────
    expected_pv_surplus_wh = sum([
        min(max(0.0, solars[t] - loads[t]), abs(OUTPUT_MIN_W)) * DT * BATTERY_CHARGE_EFF
        for t in range(N)
    ])
    pv_will_fill_battery = (E_now + expected_pv_surplus_wh) >= E_max
    pv_adjusted_headroom = max(0.0, E_max - E_now - expected_pv_surplus_wh)

    log.info(
        f"PV look-ahead: E_now={E_now:.0f}Wh "
        f"expected_pv_surplus={expected_pv_surplus_wh:.0f}Wh "
        f"pv_will_fill={pv_will_fill_battery} "
        f"pv_adjusted_headroom={pv_adjusted_headroom:.0f}Wh"
    )

    # ── Objective ────────────────────────────────────────────────────────
    c_obj = []
    for t in range(N):
        opp_cost = (
            (price_max - prices[t])
            * BATTERY_DISCHARGE_EFF
            * DT / 1000.0
            * effective_oc_weight
        )
        c_obj.append(
            -prices[t] * BATTERY_DISCHARGE_EFF * DT / 1000.0
            + opp_cost
            + DISCHARGE_PENALTY * DT / 1000.0
        )
    for t in range(N):
        pv_headroom_ratio = min(
            1.0,
            pv_adjusted_headroom / max(1.0, BATTERY_SIZE_WH * 0.3),
        )
        cheap_bonus = (
            max(0.0, price_avg - prices[t])
            * BATTERY_CHARGE_EFF
            * DT / 1000.0
            * effective_oc_weight
            * pv_headroom_ratio
        )
        c_obj.append(prices[t] * DT / 1000.0 - cheap_bonus)

    # ── Bounds ───────────────────────────────────────────────────────────
    p25 = _ctx.get("p25", 0.10)
    bounds = []
    for t in range(N):
        # Discharge upper bound
        if soc >= BATTERY_FULL_PCT and solars[t] >= pv_threshold:
            max_disch = float(OUTPUT_MAX_W)
        elif solars[t] >= pv_threshold:
            max_disch = max(0.0, loads[t])
        else:
            max_disch = float(OUTPUT_MAX_W)

        # General rule: do not discharge in the very cheapest 20% of price slots
        if prices[t] <= p20:
            max_disch = 0.0

        # Charge lower bound (negative)
        if soc >= GRID_CHARGE_SOC_BLOCK_PCT or pv_will_fill_battery:
            min_sp = 0.0
        elif soc >= GRID_CHARGE_SOC_CHEAP_PCT:
            min_sp = float(OUTPUT_MIN_W) if prices[t] <= p25 else 0.0
        else:
            min_sp = float(OUTPUT_MIN_W)

        bounds.append((min_sp, max_disch))

    for t in range(N):
        bounds.append((0.0, None))   # g[t] >= 0

    # ── Inequality constraints  A_ub · x ≤ b_ub ─────────────────────────
    A_ub = []
    b_ub = []

    # 1) Grid slack: -b[t] - g[t] ≤ solar[t] - load[t]
    for t in range(N):
        row = [0.0] * (2 * N)
        row[t]     = -1.0
        row[N + t] = -1.0
        A_ub.append(row)
        b_ub.append(solars[t] - loads[t])

    # 2+3) SOC lower and upper bounds
    _pv_slots    = [t for t in range(N) if solars[t] >= pv_threshold]
    last_pv_slot = _pv_slots[-1] if _pv_slots else -1
    soc_weight   = min(1.0, (E_now - E_min) / max(1.0, E_max - E_min))

    estimated_discharge_wh = sum([
        max(0.0, loads[t]) * DT
        for t in range(N)
        if t > last_pv_slot and solars[t] < pv_threshold
    ]) * soc_weight

    remaining_headroom = min(
        E_max - E_now + estimated_discharge_wh * BATTERY_DISCHARGE_EFF,
        BATTERY_SIZE_WH * (BATTERY_FULL_PCT - BATTERY_EMPTY_PCT) / 100.0,
    )

    log.info(
        f"LP headroom: E_now={E_now:.0f}Wh "
        f"last_pv_slot={last_pv_slot} "
        f"est_discharge={estimated_discharge_wh:.0f}Wh "
        f"remaining_headroom={remaining_headroom:.0f}Wh | "
        f"price_min={price_min * 100:.1f} "
        f"price_avg={price_avg * 100:.1f} "
        f"price_max={price_max * 100:.1f} ct | "
        f"OC_weight={effective_oc_weight}"
    )

    cum_surplus_e = 0.0
    for k in range(N):
        absorbed           = min(surplus[k] * DT * BATTERY_CHARGE_EFF, remaining_headroom)
        remaining_headroom = max(0.0, remaining_headroom - absorbed)
        cum_surplus_e     += absorbed

        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = DT / BATTERY_DISCHARGE_EFF
        A_ub.append(row)
        b_ub.append(E_now - E_min + cum_surplus_e)

        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = -DT * BATTERY_CHARGE_EFF
        A_ub.append(row)
        b_ub.append(max(0.0, E_max - E_now - cum_surplus_e))

    # 4) No-export constraint
    if not ALLOW_EXPORT:
        for t in range(N):
            net = loads[t] - solars[t]
            if net >= 0:
                row = [0.0] * (2 * N)
                row[t] = 1.0
                A_ub.append(row)
                b_ub.append(net)

    # ── Solve ────────────────────────────────────────────────────────────
    try:
        result = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        if result.status != 0:
            log.warning(f"LP status {result.status}: {result.message} — fallback")
            return _heuristic_schedule(soc, schedule)

        optimal = []
        e = E_now
        total_cost = 0.0
        for t in range(N):
            sp = int(round(result.x[t] / 10) * 10)
            sp = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, sp))
            optimal.append(sp)
            grid_w = loads[t] - solars[t] - sp
            if grid_w > 0:
                total_cost += grid_w * prices[t] * DT / 1000.0
            if sp > 0:
                e -= sp * DT / BATTERY_DISCHARGE_EFF
            else:
                e -= sp * DT * BATTERY_CHARGE_EFF
            e += surplus[t] * DT * BATTERY_CHARGE_EFF
            e  = max(E_min, min(E_max, e))

        log.info(
            f"LP solved ✓ | Slot-0={optimal[0]:+d}W | "
            f"24h cost={total_cost:.4f} € | "
            f"RTE={BATTERY_CHARGE_EFF * BATTERY_DISCHARGE_EFF:.0%} | "
            f"OC_weight={effective_oc_weight}"
        )
        return optimal

    except Exception as exc:
        log.error(f"LP solve error: {exc} — fallback")
        return _heuristic_schedule(soc, schedule)

# ════════════════════════════════════════════════════════════════════════════
# HEURISTIC OPTIMIZER  (fallback when LP is disabled or fails)
# ════════════════════════════════════════════════════════════════════════════

def _assess_future_value(schedule: list, p75: float) -> dict:
    high_demand_wh = 0.0
    slots = 0
    for entry in schedule[1:]:
        if entry["price"] >= p75 and entry["net"] > 0:
            high_demand_wh += entry["net"] * 0.25
            slots += 1
    return {"high_demand_wh": high_demand_wh, "slots": slots}


def _estimate_pv_recharge(schedule: list, p75: float) -> float:
    surplus_wh = 0.0
    for entry in schedule[1:]:
        if entry["net"] < 0:
            surplus_wh += abs(entry["net"]) * 0.25
        elif entry["net"] > 0 and entry["price"] >= p75:
            break
    return surplus_wh


def _heuristic_schedule(soc: float, schedule: list) -> list:
    if not schedule:
        return [0] * SCHEDULE_SLOTS

    prices_sorted = sorted([s["price"] for s in schedule])
    n   = len(prices_sorted)
    p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
    p75 = prices_sorted[min(n - 1, int(n * 0.75))]
    p20 = prices_sorted[max(0, int(n * 0.20) - 1)]

    _ctx["p25"] = p25
    _ctx["p75"] = p75

    available_wh   = max(0.0, (soc - BATTERY_EMPTY_PCT) / 100.0 * BATTERY_SIZE_WH)
    future_value   = _assess_future_value(schedule, p75)
    pv_recharge_wh = _estimate_pv_recharge(schedule, p75)

    log.info(
        f"Heuristic: P25={p25 * 100:.1f} P75={p75 * 100:.1f} ct/kWh | "
        f"Future demand={future_value['high_demand_wh']:.0f}Wh | "
        f"PV recharge={pv_recharge_wh:.0f}Wh | "
        f"Avail={available_wh:.0f}Wh"
    )

    result = []
    for s in schedule:
        price    = s["price"]
        net      = s["net"]
        net_load = max(0, int(net))

        # Never discharge in the cheapest 20% price slots
        if price <= p20 and net > 0:
            sp = 0

        elif soc <= BATTERY_EMPTY_PCT:
            sp = OUTPUT_MIN_W if price <= p25 else 0
        elif price >= p75:
            sp = min(OUTPUT_MAX_W, max(0, int(net)))
        elif price <= p25:
            if soc < GRID_CHARGE_SOC_BLOCK_PCT:
                sp = OUTPUT_MIN_W
            else:
                sp = 0
        else:
            if future_value["high_demand_wh"] > 0:
                if available_wh >= future_value["high_demand_wh"]:
                    sp = (
                        min(int(net), net_load)
                        if pv_recharge_wh >= future_value["high_demand_wh"]
                        else max(0, int(net - available_wh))
                    )
                else:
                    sp = min(int(net), net_load)
            else:
                sp = min(int(net), net_load)
        result.append(sp)

    return result

# ════════════════════════════════════════════════════════════════════════════
# SCHEDULE DISPATCHER + MODE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_schedule(soc: float, schedule: list) -> list:
    if USE_LP_OPTIMIZER:
        log.info("Using LP optimizer")
        return _solve_optimal_schedule(soc, schedule)
    log.info("Using heuristic optimizer")
    return _heuristic_schedule(soc, schedule)


def _mode_from_setpoint(sp: int) -> str:
    if sp < -GRID_DEADZONE_W:
        return "GRID_CHARGE"
    if sp > GRID_DEADZONE_W:
        return "DISCHARGE"
    return "BALANCE"


def _apply_trickle_override(soc: float, sp: int, net: float, price: float) -> tuple:
    """
    Post-process the LP/heuristic setpoint with real-time SOC guards.
    """
    p75 = _ctx.get("p75", 0.20)
    p25 = _ctx.get("p25", 0.10)

    if soc >= BATTERY_FULL_PCT:
        pv_surplus = max(0.0, -net)
        if pv_surplus > GRID_DEADZONE_W:
            sp_anti_curtail = min(int(round(pv_surplus / 10) * 10), OUTPUT_MAX_W)
            log.info(
                f"Anti-curtail discharge: PV surplus={pv_surplus:.0f}W "
                f"→ setpoint={sp_anti_curtail:+d}W (SOC {soc:.0f}%)"
            )
            return ("DISCHARGE", sp_anti_curtail)
        if sp > GRID_DEADZONE_W:
            return (_mode_from_setpoint(sp), sp)
        return ("TRICKLE", 0)

    if soc >= BATTERY_TRICKLE_PCT:
        if net < -GRID_DEADZONE_W:
            return ("BALANCE", 0)
        return ("TRICKLE", -BATTERY_TRICKLE_W)

    if sp < -GRID_DEADZONE_W:
        if soc >= GRID_CHARGE_SOC_BLOCK_PCT:
            log.info(f"Grid-charge suppressed: SOC {soc:.0f}% >= {GRID_CHARGE_SOC_BLOCK_PCT}%")
            return ("BALANCE", 0)
        if soc >= GRID_CHARGE_SOC_CHEAP_PCT and price > p25:
            log.info(
                f"Grid-charge suppressed: SOC {soc:.0f}% >= {GRID_CHARGE_SOC_CHEAP_PCT}% "
                f"and price {price * 100:.1f} ct > p25 {p25 * 100:.1f} ct"
            )
            return ("BALANCE", 0)

    if price >= p75:
        return (_mode_from_setpoint(sp), sp)

    return (_mode_from_setpoint(sp), sp)

# ════════════════════════════════════════════════════════════════════════════
# HA OUTPUT HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _write_outputs(mode: str, sp: int):
    mode_id = MODE_IDS.get(mode, 0)
    input_number.set_value(entity_id=E_MODE_ID,  value=mode_id)
    input_number.set_value(entity_id=E_SETPOINT, value=sp)
    log.info(f"Output → mode_id={mode_id} ({mode}) setpoint={sp:+d}W")


def _update_status(mode: str, reason: str):
    mode_icons = {
        "GRID_CHARGE": "⚡ GRID CHARGE",
        "DISCHARGE":   "🔋 DISCHARGE",
        "BALANCE":     "🏭 GRID CONSUMPTION",
        "TRICKLE":     "🌿 TRICKLE",
    }
    label = mode_icons.get(mode, mode)
    input_text.set_value(entity_id=E_STATUS_MODE,   value=label)
    input_text.set_value(entity_id=E_STATUS_REASON, value=reason)
    log.info(f"Status: {label} | {reason}")

# (Rest of file: outlook, CSV, Influx logging, triggers, and services remain as in your original.)
