# pyscript/energy_optimizer.py
"""
Energy Optimizer — pyscript (HACS)

Two-layer control:
  Strategic (15 min): InfluxDB history + Solcast + EPEX → optimal or heuristic schedule
  Tactical   (5 sec): real-time proportional controller targeting grid_power ≈ 0

No feed-in tariff — export only occurs to prevent PV curtailment when battery is full.
Trickle hysteresis keeps battery floating between BATTERY_TRICKLE_PCT and BATTERY_FULL_PCT.

Two optimization modes (set USE_LP_OPTIMIZER below):
  True  → Linear Program (scipy HiGHS) — globally cost-optimal 96-slot schedule
  False → Heuristic (P25/P75 percentile rules + forward-looking battery reservation)

Requires in configuration.yaml:
  pyscript:
    allow_all_imports: true

  input_text:
    energy_optimizer_mode:
      name: Energy Optimizer Mode
      max: 32
      icon: mdi:battery-charging
    energy_optimizer_reason:
      name: Energy Optimizer Reason
      max: 255
      icon: mdi:information-outline

Only file needed:
  /config/pyscript/energy_optimizer.py
"""

import aiohttp
import pytz
from datetime import datetime, timedelta

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

# ── Optimization mode ─────────────────────────────────────────────────────
USE_LP_OPTIMIZER = True    # True = Linear Program | False = Heuristic P25/P75

# ── Hardware constants ────────────────────────────────────────────────────
BATTERY_SIZE_WH      = 2760
OUTPUT_MIN_W         = -1200
OUTPUT_MAX_W         =  1200
BATTERY_FULL_PCT     =  98
BATTERY_TRICKLE_PCT  =  96
BATTERY_EMPTY_PCT    =  15
GRID_DEADZONE_W      =  10
BATTERY_TRICKLE_W    =  10

# ── Behaviour flags ───────────────────────────────────────────────────────
ALLOW_EXPORT = False       # True only if you have a feed-in tariff

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
INFLUX_UNIT   = "W"        # change to "kW" if sensor reports in kW

# ── HA entity IDs ─────────────────────────────────────────────────────────
E_GRID_POWER     = "sensor.shrdzm_485519e15aae_16_7_0"
E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER  = "sensor.ezhi_battery_power"
E_INV_OUTPUT     = "number.apsystems_ezhi_max_output_power"
E_PRICE_DATA     = "sensor.epex_spot_data_total_price"
E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

# ── Dashboard input_text helpers ──────────────────────────────────────────
E_STATUS_MODE   = "input_text.energy_optimizer_mode"
E_STATUS_REASON = "input_text.energy_optimizer_reason"

# ── Timezone ──────────────────────────────────────────────────────────────
TZ = pytz.timezone("Europe/Vienna")

# ── Shared context between strategic and tactical layers ──────────────────
_ctx = {
    "mode":             "BALANCE",  # GRID_CHARGE | DISCHARGE | BALANCE | TRICKLE
    "setpoint":         0,
    "strategic_sp":     0,
    "price":            0.15,
    "p25":              0.10,
    "p75":              0.20,
    "last_soc":         50.0,
    "optimal_schedule": [],
    "schedule_time":    None,
}


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
    hourly = [150, 150, 150, 150, 150, 150,
              600, 600, 600,
              350, 350, 350, 350, 350,
              350, 350, 350, 350,
              700, 700, 700, 700, 700,
              300, 300]
    result = {}
    for h in range(24):
        for q in range(4):
            result[(h, q)] = hourly[h]
    return result


# ════════════════════════════════════════════════════════════════════════════
# SENSOR HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_solar_forecast() -> dict:
    """Returns {hour: mean_watts} from Solcast sensor attributes."""
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
    """Returns {(hour, quarter_idx): EUR/kWh} from EPEX sensor attribute 'data'."""
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
    """
    Assembles 96 forward-looking 15-min slots from the current timestamp.
    net < 0 → PV surplus | net > 0 → deficit
    """
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


# ════════════════════════════════════════════════════════════════════════════
# OPTION A — LINEAR PROGRAM OPTIMIZER (USE_LP_OPTIMIZER = True)
# ════════════════════════════════════════════════════════════════════════════

def _solve_optimal_schedule(soc: float, schedule: list) -> list:
    """
    Solves a Linear Program (scipy HiGHS) for the cost-minimizing inverter
    setpoint across all 96 slots simultaneously.

    Variable vector: [x_0..x_N-1, s_0..s_N-1]  (length = 2N)
      x[t] = inverter output W  (negative=charge, positive=discharge)
      s[t] = grid import W      (slack, >= 0)

    Objective: minimize sum(price[t] * s[t] * DT)

    Constraints:
      s[t] >= load[t] - solar[t] - x[t]    (grid import covers deficit)
      s[t] >= 0                              (no negative import)
      E_min <= E_now - sum(x[0..k])*DT <= E_max  for all k
      x[t] in [OUTPUT_MIN_W, OUTPUT_MAX_W]
    """
    try:
        from scipy.optimize import linprog
        import numpy as np
    except Exception as exc:
        log.error(f"scipy/numpy import failed: {exc} — falling back to heuristic")
        return _heuristic_schedule(soc, schedule)

    N     = len(schedule)
    DT    = 0.25
    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH

    loads  = []
    solars = []
    prices = []
    for s in schedule:
        loads.append(s["cons"])
        solars.append(s["solar"])
        prices.append(s["price"])

    # ── Objective ─────────────────────────────────────────────────────────
    c_obj = []
    for t in range(N):
        c_obj.append(0.0)
    for t in range(N):
        c_obj.append(prices[t] * DT / 1000.0)

    # ── Bounds ────────────────────────────────────────────────────────────
    bounds = []
    for t in range(N):
        bounds.append((float(OUTPUT_MIN_W), float(OUTPUT_MAX_W)))
    for t in range(N):
        bounds.append((0.0, None))

    # ── Inequality constraints A_ub @ vars <= b_ub ────────────────────────
    A_ub = []
    b_ub = []

    # Grid import slack: -x[t] - s[t] <= solar[t] - load[t]
    for t in range(N):
        row = [0.0] * (2 * N)
        row[t]     = -1.0
        row[N + t] = -1.0
        A_ub.append(row)
        b_ub.append(solars[t] - loads[t])

    # Battery bounds (cumulative energy balance)
    for k in range(N):
        # Don't over-discharge: sum(x[0..k])*DT <= E_now - E_min
        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = DT
        A_ub.append(row)
        b_ub.append(E_now - E_min)

        # Don't over-charge: -sum(x[0..k])*DT <= E_max - E_now
        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = -DT
        A_ub.append(row)
        b_ub.append(E_max - E_now)

    try:
        result = linprog(
            c_obj,
            A_ub=A_ub,
            b_ub=b_ub,
            bounds=bounds,
            method="highs",
        )

        if result.status != 0:
            log.warning(
                f"LP solver status {result.status}: {result.message} "
                f"— falling back to heuristic"
            )
            return _heuristic_schedule(soc, schedule)

        # Extract and round setpoints
        optimal = []
        e       = E_now
        for t in range(N):
            raw = result.x[t]
            sp  = int(round(raw / RT_ROUND_W) * RT_ROUND_W)
            sp  = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, sp))
            if sp < 0 and e < E_max * 0.99:
                sp = max(sp, 0)
            optimal.append(sp)
            e = e - sp * DT

        # Compute expected 24h cost for logging
        total_cost = 0.0
        e = E_now
        for t in range(N):
            grid_w = loads[t] - solars[t] - optimal[t]
            if grid_w > 0:
                total_cost += grid_w * prices[t] * DT / 1000.0
            e = e - optimal[t] * DT

        log.info(
            f"LP solved ✓ | Slot-0={optimal[0]:+d}W | "
            f"Expected 24h grid cost={total_cost:.4f} €"
        )
        return optimal

    except Exception as exc:
        log.error(f"LP solve error: {exc} — falling back to heuristic")
        return _heuristic_schedule(soc, schedule)


# ════════════════════════════════════════════════════════════════════════════
# OPTION B — HEURISTIC OPTIMIZER (USE_LP_OPTIMIZER = False)
# ════════════════════════════════════════════════════════════════════════════

def _assess_future_value(schedule: list, p75: float) -> dict:
    """
    Scans forward slots for windows where price >= P75 AND net > 0.
    Returns total Wh demand and slot count in those windows.
    """
    high_demand_wh = 0.0
    slots          = 0
    for entry in schedule[1:]:
        if entry["price"] >= p75 and entry["net"] > 0:
            high_demand_wh += entry["net"] * 0.25
            slots          += 1
    return {"high_demand_wh": high_demand_wh, "slots": slots}


def _estimate_pv_recharge(schedule: list, p75: float) -> float:
    """
    Estimates Wh of PV surplus available in future slots before the first
    expensive+high-demand window.
    """
    surplus_wh = 0.0
    for entry in schedule[1:]:
        if entry["net"] < 0:
            surplus_wh += abs(entry["net"]) * 0.25
        elif entry["net"] > 0 and entry["price"] >= p75:
            break
    return surplus_wh


def _heuristic_schedule(soc: float, schedule: list) -> list:
    """
    Builds a 96-slot setpoint list using P25/P75 heuristic rules with
    forward-looking battery reservation for mid-price slots.
    """
    if not schedule:
        result = []
        for i in range(96):
            result.append(0)
        return result

    prices_list = []
    for s in schedule:
        prices_list.append(s["price"])
    prices_sorted = sorted(prices_list)
    n   = len(prices_sorted)
    p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
    p75 = prices_sorted[min(n - 1, int(n * 0.75))]

    _ctx["p25"] = p25
    _ctx["p75"] = p75

    available_wh   = max(0.0, (soc - BATTERY_EMPTY_PCT) / 100.0 * BATTERY_SIZE_WH)
    future_value   = _assess_future_value(schedule, p75)
    pv_recharge_wh = _estimate_pv_recharge(schedule, p75)

    log.info(
        f"Heuristic: P25={p25:.4f} P75={p75:.4f} | "
        f"Future demand={future_value['high_demand_wh']:.0f}Wh "
        f"({future_value['slots']} slots) | "
        f"PV recharge={pv_recharge_wh:.0f}Wh | "
        f"Battery avail={available_wh:.0f}Wh"
    )

    result = []
    for s in schedule:
        price = s["price"]
        net   = s["net"]

        if soc <= BATTERY_EMPTY_PCT:
            if price <= p25:
                sp = OUTPUT_MIN_W
            else:
                sp = 0
        elif price >= p75:
            sp = max(0, min(OUTPUT_MAX_W, int(net)))
        elif price <= p25:
            sp = OUTPUT_MIN_W
        else:
            if future_value["high_demand_wh"] > 0:
                if available_wh >= future_value["high_demand_wh"]:
                    if pv_recharge_wh >= future_value["high_demand_wh"]:
                        sp = int(net)
                    else:
                        sp = max(0, int(net - available_wh))
                else:
                    sp = int(net)
            else:
                sp = int(net)

        result.append(sp)

    return result


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULE DISPATCHER
# ════════════════════════════════════════════════════════════════════════════

def _get_schedule(soc: float, schedule: list) -> list:
    if USE_LP_OPTIMIZER:
        log.info("Using LP optimizer")
        return _solve_optimal_schedule(soc, schedule)
    else:
        log.info("Using heuristic optimizer")
        return _heuristic_schedule(soc, schedule)


def _mode_from_setpoint(sp: int) -> str:
    if sp < -GRID_DEADZONE_W:
        return "GRID_CHARGE"
    elif sp > GRID_DEADZONE_W:
        return "DISCHARGE"
    else:
        return "BALANCE"


# ════════════════════════════════════════════════════════════════════════════
# TRICKLE HYSTERESIS OVERRIDE
# ════════════════════════════════════════════════════════════════════════════

def _apply_trickle_override(soc: float, sp: int, net: float) -> tuple:
    """
    Returns (mode, setpoint) after applying trickle hysteresis rules.
    Overrides LP/heuristic slot-0 setpoint when SOC is near full.
    """
    if soc >= BATTERY_FULL_PCT:
        if net < -GRID_DEADZONE_W:
            spill = max(0, min(OUTPUT_MAX_W, int(net * -1)))
            return ("BALANCE", spill)
        else:
            return ("TRICKLE", BATTERY_TRICKLE_W)
    elif soc >= BATTERY_TRICKLE_PCT:
        return ("TRICKLE", -BATTERY_TRICKLE_W)
    else:
        return (_mode_from_setpoint(sp), sp)


# ════════════════════════════════════════════════════════════════════════════
# DASHBOARD STATUS UPDATE
# ════════════════════════════════════════════════════════════════════════════

def _update_status(mode: str, reason: str):
    """Write current mode and reasoning to HA input_text helpers."""
    mode_icons = {
        "GRID_CHARGE": "⚡ GRID CHARGE",
        "DISCHARGE":   "🔋 DISCHARGE",
        "BALANCE":     "⚖️ BALANCE",
        "TRICKLE":     "🌿 TRICKLE",
    }
    label = mode_icons.get(mode, mode)
    input_text.set_value(entity_id=E_STATUS_MODE,   value=label)
    input_text.set_value(entity_id=E_STATUS_REASON, value=reason)
    log.info(f"Status: {label} | {reason}")


# ════════════════════════════════════════════════════════════════════════════
# 24H OUTLOOK LOGGER
# ════════════════════════════════════════════════════════════════════════════

def _log_24h_outlook(schedule: list, optimal_schedule: list, soc: float):
    """
    Logs a human-readable 24h operational outlook by merging consecutive
    slots with the same planned action into labelled time windows.
    Called once per hour from strategic_optimize().
    """
    if not schedule or not optimal_schedule:
        log.info("Outlook: no schedule available")
        return

    p75 = _ctx.get("p75", 0.20)

    # ── Classify each slot ────────────────────────────────────────────────
    slots = []
    for i in range(len(schedule)):
        if i >= len(optimal_schedule):
            break
        s  = schedule[i]
        sp = optimal_schedule[i]
        p  = s["price"]
        n  = s["net"]

        if sp <= -GRID_DEADZONE_W:
            label = "GRID_CHARGE"
        elif sp >= GRID_DEADZONE_W and p >= p75:
            label = "DISCHARGE_PEAK"
        elif sp >= GRID_DEADZONE_W and p < p75:
            label = "DISCHARGE_MID"
        elif n < -GRID_DEADZONE_W:
            label = "PV_SURPLUS"
        else:
            label = "BALANCE"

        slots.append({
            "time":  s["time"],
            "label": label,
            "price": p,
            "net":   n,
            "sp":    sp,
        })

    # ── Merge consecutive same-label slots into windows ───────────────────
    windows = []
    if not slots:
        return

    cur_label  = slots[0]["label"]
    cur_start  = slots[0]["time"]
    cur_prices = [slots[0]["price"]]
    cur_sp     = [slots[0]["sp"]]
    cur_net    = [slots[0]["net"]]

    for slot in slots[1:]:
        if slot["label"] == cur_label:
            cur_prices.append(slot["price"])
            cur_sp.append(slot["sp"])
            cur_net.append(slot["net"])
        else:
            windows.append({
                "label":     cur_label,
                "start":     cur_start,
                "end":       slot["time"],
                "avg_price": sum(cur_prices) / len(cur_prices),
                "min_price": min(cur_prices),
                "max_price": max(cur_prices),
                "avg_sp":    sum(cur_sp) / len(cur_sp),
                "avg_net":   sum(cur_net) / len(cur_net),
                "n_slots":   len(cur_prices),
            })
            cur_label  = slot["label"]
            cur_start  = slot["time"]
            cur_prices = [slot["price"]]
            cur_sp     = [slot["sp"]]
            cur_net    = [slot["net"]]

    windows.append({
        "label":     cur_label,
        "start":     cur_start,
        "end":       cur_start + timedelta(minutes=15 * len(cur_prices)),
        "avg_price": sum(cur_prices) / len(cur_prices),
        "min_price": min(cur_prices),
        "max_price": max(cur_prices),
        "avg_sp":    sum(cur_sp) / len(cur_sp),
        "avg_net":   sum(cur_net) / len(cur_net),
        "n_slots":   len(cur_prices),
    })

    # ── Build and log human-readable lines ───────────────────────────────
    label_text = {
        "GRID_CHARGE":    "⚡ Charge from grid",
        "DISCHARGE_PEAK": "🔋 Discharge (peak price)",
        "DISCHARGE_MID":  "🔋 Discharge (mid price)",
        "PV_SURPLUS":     "☀️  PV surplus / spill",
        "BALANCE":        "⚖️  Follow grid / self-consume",
    }

    lines = []
    lines.append(
        f"─── 24h Outlook ({'LP' if USE_LP_OPTIMIZER else 'Heuristic'}) | "
        f"SOC {soc:.0f}% | "
        f"P25={_ctx.get('p25', 0):.4f} P75={_ctx.get('p75', 0):.4f} €/kWh ───"
    )

    for w in windows:
        start_str = w["start"].strftime("%H:%M")
        end_str   = w["end"].strftime("%H:%M")
        duration  = w["n_slots"] * 15
        desc      = label_text.get(w["label"], w["label"])

        if abs(w["min_price"] - w["max_price"]) < 0.0001:
            price_str = f"{w['avg_price']:.4f} €/kWh"
        else:
            price_str = (
                f"{w['avg_price']:.4f} €/kWh "
                f"(range {w['min_price']:.4f}–{w['max_price']:.4f})"
            )

        if w["label"] == "GRID_CHARGE":
            detail = f"avg {w['avg_sp']:.0f}W from grid"
        elif w["label"] in ("DISCHARGE_PEAK", "DISCHARGE_MID"):
            detail = f"avg {w['avg_sp']:.0f}W output | net demand {w['avg_net']:.0f}W"
        elif w["label"] == "PV_SURPLUS":
            detail = f"avg {abs(w['avg_net']):.0f}W surplus"
        else:
            detail = f"avg net load {w['avg_net']:+.0f}W"

        lines.append(
            f"  {start_str}–{end_str} ({duration:3d}min)  "
            f"{desc:<32}  {price_str:<48}  {detail}"
        )

    lines.append(
        "────────────────────────────────────────────────────────────────"
    )

    for line in lines:
        log.info(line)


# ════════════════════════════════════════════════════════════════════════════
# APPLY SETPOINT
# ════════════════════════════════════════════════════════════════════════════

def _apply_if_changed(new_sp: int):
    """Write setpoint to HA only when it differs by >= RT_MIN_DELTA W."""
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
    log.info(
        f"── Strategic optimization cycle "
        f"({'LP' if USE_LP_OPTIMIZER else 'Heuristic'}) ──"
    )
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

        schedule         = _build_schedule(consumption, solar, prices)
        optimal_schedule = _get_schedule(soc, schedule)

        _ctx["optimal_schedule"] = optimal_schedule
        _ctx["schedule_time"]    = datetime.now(TZ)

        raw_sp = optimal_schedule[0] if optimal_schedule else 0
        net    = schedule[0]["net"]   if schedule         else 0.0
        price  = schedule[0]["price"] if schedule         else 0.15

        mode, sp = _apply_trickle_override(soc, raw_sp, net)

        _ctx["mode"]         = mode
        _ctx["strategic_sp"] = sp
        _ctx["price"]        = price

        if mode not in ("BALANCE", "TRICKLE"):
            _apply_if_changed(sp)

        # ── Build dashboard reason string ─────────────────────────────────
        p25 = _ctx.get("p25", 0.10)
        p75 = _ctx.get("p75", 0.20)

        if mode == "GRID_CHARGE":
            reason = (
                f"Price {price:.4f} €/kWh is in cheapest 25% (≤ {p25:.4f}). "
                f"Charging battery at max rate ({OUTPUT_MIN_W}W) to store cheap "
                f"energy for later. SOC: {soc:.0f}%."
            )
        elif mode == "DISCHARGE":
            reason = (
                f"Price {price:.4f} €/kWh is in most expensive 25% (≥ {p75:.4f}). "
                f"Discharging battery ({sp:+d}W) to cover apartment load and avoid "
                f"buying expensive grid power. SOC: {soc:.0f}%."
            )
        elif mode == "TRICKLE" and soc >= BATTERY_FULL_PCT:
            reason = (
                f"Battery full ({soc:.0f}%). Gently increasing output to prevent "
                f"trickle charging above {BATTERY_FULL_PCT}%. "
                f"Holding SOC in {BATTERY_TRICKLE_PCT}–{BATTERY_FULL_PCT}% band. "
                f"Price: {price:.4f} €/kWh."
            )
        elif mode == "TRICKLE":
            reason = (
                f"SOC {soc:.0f}% is in hysteresis band "
                f"({BATTERY_TRICKLE_PCT}–{BATTERY_FULL_PCT}%). "
                f"Gently recharging to keep battery near full. "
                f"Price: {price:.4f} €/kWh."
            )
        elif mode == "BALANCE" and soc >= BATTERY_FULL_PCT and net < -GRID_DEADZONE_W:
            reason = (
                f"Battery full ({soc:.0f}%) with PV surplus of {abs(net):.0f}W. "
                f"Spilling minimum surplus ({sp:+d}W) to grid to prevent PV "
                f"curtailment. Price: {price:.4f} €/kWh."
            )
        else:
            if USE_LP_OPTIMIZER:
                reason = (
                    f"LP optimizer: no strong charge/discharge signal at "
                    f"{price:.4f} €/kWh [P25={p25:.4f} P75={p75:.4f}]. "
                    f"Following grid in real time. SOC: {soc:.0f}%. "
                    f"Slot-0 guidance: {raw_sp:+d}W."
                )
            else:
                fv       = _assess_future_value(schedule, p75)
                pvc      = _estimate_pv_recharge(schedule, p75)
                avail_wh = max(0.0, (soc - BATTERY_EMPTY_PCT) / 100.0 * BATTERY_SIZE_WH)
                if (fv["high_demand_wh"] > 0
                        and avail_wh >= fv["high_demand_wh"]
                        and pvc < fv["high_demand_wh"]):
                    reason = (
                        f"Mid price ({price:.4f} €/kWh). Holding battery for upcoming "
                        f"expensive window ({fv['slots']} slots, "
                        f"{fv['high_demand_wh']:.0f}Wh needed). "
                        f"PV recharge forecast ({pvc:.0f}Wh) insufficient. "
                        f"SOC: {soc:.0f}% ({avail_wh:.0f}Wh available)."
                    )
                elif fv["high_demand_wh"] > 0:
                    reason = (
                        f"Mid price ({price:.4f} €/kWh). Expensive window ahead "
                        f"({fv['slots']} slots, {fv['high_demand_wh']:.0f}Wh). "
                        f"PV forecast ({pvc:.0f}Wh) will recharge battery in time — "
                        f"discharging freely. SOC: {soc:.0f}%."
                    )
                else:
                    reason = (
                        f"Mid price ({price:.4f} €/kWh). No high-value window ahead. "
                        f"Self-consuming PV, following grid in real time. "
                        f"SOC: {soc:.0f}%."
                    )

        _update_status(mode, reason)

        log.info(
            f"Mode={mode} | SOC={soc:.0f}% | "
            f"Price={price:.4f} €/kWh | "
            f"Optimizer={raw_sp:+d}W → Applied={sp:+d}W"
        )

        # Log 24h outlook once per hour (first cycle of each hour)
        now = datetime.now(TZ)
        if now.minute < 15:
            _log_24h_outlook(schedule, optimal_schedule, soc)

    except Exception as exc:
        import traceback
        log.error(f"Strategic error: {exc}\n{traceback.format_exc()}")


# ════════════════════════════════════════════════════════════════════════════
# TACTICAL LAYER — every 5 seconds
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("period(now, 5s)")
def realtime_control():
    """
    Every 5 seconds:

    TRICKLE:
      Nudges setpoint ±BATTERY_TRICKLE_W based on live SOC and battery power.
      Exits to BALANCE when SOC drops below BATTERY_TRICKLE_PCT.

    GRID_CHARGE / DISCHARGE:
      Re-asserts strategic setpoint (guards against inverter resets).

    BALANCE:
      Proportional controller: new_sp = current_sp + RT_GAIN * grid_power
      Targets grid_power ≈ 0. Respects SOC safety guards and no-export rule.
    """
    try:
        mode = _ctx["mode"]

        # ── TRICKLE ───────────────────────────────────────────────────────
        if mode == "TRICKLE":
            soc_raw  = state.get(E_BATTERY_SOC)
            batt_raw = state.get(E_BATTERY_POWER)
            soc      = float(soc_raw)  if soc_raw  not in (None, "unavailable", "unknown") else _ctx["last_soc"]
            batt_w   = float(batt_raw) if batt_raw not in (None, "unavailable", "unknown") else 0.0
            _ctx["last_soc"] = soc

            if soc >= BATTERY_FULL_PCT and batt_w > 0:
                _apply_if_changed(_ctx["setpoint"] + BATTERY_TRICKLE_W)
            elif soc < BATTERY_TRICKLE_PCT:
                _ctx["mode"] = "BALANCE"
                _update_status(
                    "BALANCE",
                    f"Trickle complete. SOC {soc:.0f}% dropped below "
                    f"{BATTERY_TRICKLE_PCT}% — returning to real-time grid following."
                )
                log.info(f"Trickle: SOC {soc:.0f}% < {BATTERY_TRICKLE_PCT}% → BALANCE")
            return

        # ── GRID_CHARGE / DISCHARGE ───────────────────────────────────────
        if mode != "BALANCE":
            _apply_if_changed(_ctx["strategic_sp"])
            return

        # ── BALANCE ───────────────────────────────────────────────────────
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
        if new_sp < 0 and soc < BATTERY_FULL_PCT:
            new_sp = 0

        _apply_if_changed(new_sp)

    except Exception as exc:
        log.error(f"Realtime control error: {exc}")


# ════════════════════════════════════════════════════════════════════════════
# EVENT TRIGGERS
# ════════════════════════════════════════════════════════════════════════════

@state_trigger(E_PRICE_DATA)
async def on_price_update(**kwargs):
    """Re-run strategic cycle immediately when EPEX prices update (~17:00)."""
    log.info("EPEX price data updated — triggering strategic cycle")
    await strategic_optimize()


@state_trigger(E_BATTERY_SOC)
def on_soc_critical(**kwargs):
    """Force inverter to 0 W if SOC drops below hard floor (12%)."""
    soc_raw = state.get(E_BATTERY_SOC)
    if soc_raw in (None, "unavailable", "unknown"):
        return
    if float(soc_raw) < 12:
        number.set_value(entity_id=E_INV_OUTPUT, value=0)
        _ctx["mode"]         = "BALANCE"
        _ctx["setpoint"]     = 0
        _ctx["strategic_sp"] = 0
        _update_status(
            "BALANCE",
            f"⚠️ Emergency: SOC critically low ({soc_raw}%). "
            f"Inverter forced to 0W. Optimizer resumes at next 15-min cycle."
        )
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
