# pyscript/energy_optimizer.py
"""
Energy Optimizer — pyscript (HACS)  —  Strategic planning layer only.

Runs every 30 minutes (and on EPEX price update events).
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

  # Lovelace Markdown card:
  #   type: markdown
  #   title: ⚡ Energy Optimizer — 24h Outlook
  #   content: "{{ state_attr('sensor.energy_optimizer_outlook', 'content') }}"

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

USE_LP_OPTIMIZER     = True

BATTERY_SIZE_WH      = 2760
OUTPUT_MIN_W         = -1200
OUTPUT_MAX_W         =  1200
BATTERY_FULL_PCT     =  98
BATTERY_TRICKLE_PCT  =  96
BATTERY_EMPTY_PCT    =  15
GRID_DEADZONE_W      =  10
BATTERY_TRICKLE_W    =  10

DISCHARGE_PENALTY    = 0.0001

ALLOW_EXPORT         = False

INFLUX_URL           = "http://localhost:8086/query"
INFLUX_DB            = "homeassistant"
INFLUX_USER          = "homeassistant"
INFLUX_PASS          = "hainflux!"
INFLUX_ENTITY        = "total_consumption"
INFLUX_UNIT          = "W"

E_BATTERY_SOC        = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER      = "sensor.ezhi_battery_power"
E_PRICE_DATA         = "sensor.epex_spot_data_total_price"
E_SOLAR_HOUR         = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TODAY        = "sensor.solcast_pv_forecast_forecast_today"
E_SOLAR_TOMORROW     = "sensor.solcast_pv_forecast_forecast_tomorrow"

E_MODE_ID            = "input_number.energy_optimizer_mode_id"
E_SETPOINT           = "input_number.energy_optimizer_setpoint"
MODE_IDS = {
    "BALANCE":     0,
    "GRID_CHARGE": 1,
    "DISCHARGE":   2,
    "TRICKLE":     3,
}

E_STATUS_MODE        = "input_text.energy_optimizer_mode"
E_STATUS_REASON      = "input_text.energy_optimizer_reason"

OUTLOOK_FILE         = "/config/www/energy_outlook.md"

TZ                   = pytz.timezone("Europe/Vienna")

_ctx = {
    "p25":           0.10,
    "p75":           0.20,
    "last_schedule": [],
}


# ════════════════════════════════════════════════════════════════════════════
# INFLUXDB
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
# SOLAR FORECAST
# ════════════════════════════════════════════════════════════════════════════

def _get_solar_forecast() -> dict:
    solar = {}
    now   = datetime.now(TZ)

    def _parse_hourly(fl, filter_date=None):
      result = {}
      for entry in fl:
          t_raw = entry.get("period_start")
          pv_kw = float(entry.get("pv_estimate") or 0)
          if t_raw is None:
              continue
          # ── DEBUG: log first entry only ──
          if not result:
              log.info(f"Solar debug: type={type(t_raw).__name__} repr={repr(t_raw)} tzinfo={getattr(t_raw, 'tzinfo', 'N/A')}")
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
          if not result:
              log.info(f"Solar debug: parsed t={t} date={t.date()} hour={t.hour}")
          if filter_date is not None and t.date() != filter_date:
              continue
          result[t.hour] = pv_kw * 1000
      return result

    # Load today's remaining hours
    try:
        attrs  = state.getattr(E_SOLAR_TODAY) or {}
        fl     = attrs.get("detailedHourly") or []
        parsed = _parse_hourly(fl, filter_date=now.date())
        count  = 0
        for h, w in parsed.items():
            if h >= now.hour:
                solar[h] = w
                count += 1
        log.info(f"Solar today: {count} remaining hours loaded")
    except Exception as exc:
        log.warning(f"Solar today error: {exc}")

    # Fill tomorrow's hours
    try:
        tomorrow = (now + timedelta(days=1)).date()
        attrs    = state.getattr(E_SOLAR_TOMORROW) or {}
        fl       = attrs.get("detailedHourly") or []
        parsed   = _parse_hourly(fl, filter_date=tomorrow)
        count    = 0
        for h, w in parsed.items():
            if h not in solar:
                solar[h] = w
                count += 1
        log.info(f"Solar tomorrow: {count} hours filled")
    except Exception as exc:
        log.warning(f"Solar tomorrow error: {exc}")

    if solar:
        peak_hour = None
        peak_val  = -1
        for h, w in solar.items():
            if w > peak_val:
                peak_val  = w
                peak_hour = h
        log.info(
            f"Solar forecast: {len(solar)} hours, "
            f"peak {peak_val:.0f}W at {peak_hour:02d}:00"
        )
        for h in sorted(solar.keys()):
            if solar[h] > 0:
                log.info(f"  solar {h:02d}:00 = {solar[h]:.0f}W")
        return solar

    # Last resort scalar fallback
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
        s   = solar.get(t.hour, 0.0)
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
# LP OPTIMIZER
# ════════════════════════════════════════════════════════════════════════════

def _solve_optimal_schedule(soc: float, schedule: list) -> list:
    N     = len(schedule)
    DT    = 0.25
    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH

    loads  = [s["cons"]  for s in schedule]
    solars = [s["solar"] for s in schedule]
    prices = [s["price"] for s in schedule]

    # Variables: b[0..N-1] = battery setpoint (+ = discharge, - = grid charge)
    #            g[0..N-1] = grid import slack (always >= 0)
    #
    # Objective: minimise total grid cost
    #   b[t] > 0 (discharge): saves grid import → reward  = -prices[t]
    #   b[t] < 0 (charge):    costs grid import → penalty = +prices[t]
    #   g[t]:                 direct grid import cost
    c_obj = []
    for t in range(N):
        c_obj.append(-prices[t] * DT / 1000.0 + DISCHARGE_PENALTY * DT / 1000.0)
    for t in range(N):
        c_obj.append(prices[t] * DT / 1000.0)

    # Bounds: b in [OUTPUT_MIN_W, OUTPUT_MAX_W], g >= 0
    bounds = []
    for t in range(N):
        bounds.append((float(OUTPUT_MIN_W), float(OUTPUT_MAX_W)))
    for t in range(N):
        bounds.append((0.0, None))

    # Constraints:
    # 1) Grid import slack: g[t] >= load[t] - solar[t] - b[t]
    #    → -b[t] - g[t] <= solar[t] - load[t]
    # 2) SOC lower bound:  sum(b[0..k]) * DT <= E_now - E_min
    # 3) SOC upper bound: -sum(b[0..k]) * DT <= E_max - E_now
    # 4) No export: b[t] <= load[t] - solar[t]  when net >= 0
    A_ub = []
    b_ub = []

    for t in range(N):
        row        = [0.0] * (2 * N)
        row[t]     = -1.0
        row[N + t] = -1.0
        A_ub.append(row)
        b_ub.append(solars[t] - loads[t])

    for k in range(N):
        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = DT
        A_ub.append(row)
        b_ub.append(E_now - E_min)

        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = -DT
        A_ub.append(row)
        b_ub.append(E_max - E_now)

    if not ALLOW_EXPORT:
        for t in range(N):
            net = loads[t] - solars[t]
            if net >= 0:
                row    = [0.0] * (2 * N)
                row[t] = 1.0
                A_ub.append(row)
                b_ub.append(net)

    try:
        result = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        if result.status != 0:
            log.warning(f"LP status {result.status}: {result.message} — fallback")
            return _heuristic_schedule(soc, schedule)

        optimal = []
        e       = E_now
        for t in range(N):
            sp = int(round(result.x[t] / 10) * 10)
            sp = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, sp))
            optimal.append(sp)
            e -= sp * DT

        total_cost = 0.0
        e = E_now
        for t in range(N):
            grid_w = loads[t] - solars[t] - optimal[t]
            if grid_w > 0:
                total_cost += grid_w * prices[t] * DT / 1000.0
            e -= optimal[t] * DT

        log.info(f"LP solved ✓ | Slot-0={optimal[0]:+d}W | 24h cost={total_cost:.4f} €")
        return optimal

    except Exception as exc:
        log.error(f"LP solve error: {exc} — fallback")
        return _heuristic_schedule(soc, schedule)



# ════════════════════════════════════════════════════════════════════════════
# HEURISTIC OPTIMIZER
# ════════════════════════════════════════════════════════════════════════════

def _assess_future_value(schedule: list, p75: float) -> dict:
    high_demand_wh = 0.0
    slots          = 0
    for entry in schedule[1:]:
        if entry["price"] >= p75 and entry["net"] > 0:
            high_demand_wh += entry["net"] * 0.25
            slots          += 1
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
        return [0] * 96

    prices_sorted = sorted(s["price"] for s in schedule)
    n   = len(prices_sorted)
    p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
    p75 = prices_sorted[min(n - 1, int(n * 0.75))]

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
        if soc <= BATTERY_EMPTY_PCT:
            sp = OUTPUT_MIN_W if price <= p25 else 0
        elif price >= p75:
            sp = min(max(0, min(OUTPUT_MAX_W, int(net))), net_load)
        elif price <= p25:
            sp = OUTPUT_MIN_W
        else:
            if future_value["high_demand_wh"] > 0:
                if available_wh >= future_value["high_demand_wh"]:
                    sp = min(int(net), net_load) if pv_recharge_wh >= future_value["high_demand_wh"] else max(0, int(net - available_wh))
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


def _apply_trickle_override(soc: float, sp: int, net: float) -> tuple:
    if soc >= BATTERY_FULL_PCT:
        if net < -GRID_DEADZONE_W:
            return ("BALANCE", max(0, min(OUTPUT_MAX_W, int(net * -1))))
        else:
            return ("TRICKLE", BATTERY_TRICKLE_W)
    elif soc >= BATTERY_TRICKLE_PCT:
        return ("TRICKLE", -BATTERY_TRICKLE_W)
    else:
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


# ════════════════════════════════════════════════════════════════════════════
# 24H OUTLOOK
# ════════════════════════════════════════════════════════════════════════════

async def _log_24h_outlook(schedule: list, optimal_schedule: list, soc: float):
    if not schedule or not optimal_schedule:
        log.info("Outlook: no schedule available")
        return

    p75   = _ctx.get("p75", 0.20)
    DT    = 0.25
    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH

    slots = []
    e     = E_now
    for i in range(min(len(schedule), len(optimal_schedule))):
        s  = schedule[i]
        sp = optimal_schedule[i]
        p  = s["price"]
        n  = s["net"]

        if sp > 0:
            available_wh = max(0.0, e - E_min)
            e_after      = e - min(sp * DT, available_wh)
        else:
            e_after = e - sp * DT

        e_after   = max(E_min, min(E_max, e_after))
        soc_after = e_after / BATTERY_SIZE_WH * 100.0

        if sp <= -GRID_DEADZONE_W:
            label = "GRID_CHARGE"
        elif sp >= GRID_DEADZONE_W and p >= p75:
            label = "DISCHARGE_PEAK"
        elif sp >= GRID_DEADZONE_W and p < p75:
            label = "COVER_LOAD"
        elif n < -GRID_DEADZONE_W:
            label = "PV_SURPLUS"
        else:
            label = "GRID_CONSUMPTION"

        slots.append({
            "time":    s["time"],
            "label":   label,
            "price":   p,
            "cons_w":  s["cons"],
            "pv_w":    s["solar"],
            "batt_w":  sp,
            "grid_w":  max(0.0, n - sp),
            "soc_pct": soc_after,
        })
        e = e_after

    if not slots:
        return

    def _new_window(slot):
        return {
            "label":   slot["label"],
            "start":   slot["time"],
            "prices":  [slot["price"]],
            "cons_w":  [slot["cons_w"]],
            "pv_w":    [slot["pv_w"]],
            "batt_w":  [slot["batt_w"]],
            "grid_w":  [slot["grid_w"]],
            "soc_pct": [slot["soc_pct"]],
            "n_slots": 1,
        }

    windows = []
    cur     = _new_window(slots[0])

    for slot in slots[1:]:
        if slot["label"] == cur["label"]:
            cur["prices"].append(slot["price"])
            cur["cons_w"].append(slot["cons_w"])
            cur["pv_w"].append(slot["pv_w"])
            cur["batt_w"].append(slot["batt_w"])
            cur["grid_w"].append(slot["grid_w"])
            cur["soc_pct"].append(slot["soc_pct"])
            cur["n_slots"] += 1
        else:
            cur["end"] = slot["time"]
            windows.append(cur)
            cur = _new_window(slot)

    cur["end"] = cur["start"] + timedelta(minutes=15 * cur["n_slots"])
    windows.append(cur)

    def _avg(lst):
        return sum(lst) / len(lst)

    label_text = {
        "GRID_CHARGE":      "⚡ Charge from grid",
        "DISCHARGE_PEAK":   "🔋 Discharge (peak price)",
        "COVER_LOAD":       "⚖️  Cover load",
        "PV_SURPLUS":       "☀️  PV surplus / spill",
        "GRID_CONSUMPTION": "🏭 Grid consumption",
    }

    log_lines = []
    log_lines.append(
        f"─── 24h Outlook ({'LP' if USE_LP_OPTIMIZER else 'Heuristic'}) | "
        f"SOC {soc:.0f}% | "
        f"P25={_ctx.get('p25', 0) * 100:.1f} "
        f"P75={_ctx.get('p75', 0) * 100:.1f} ct/kWh ───"
    )

    now_str  = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    md_lines = []
    md_lines.append(
        f"**{'LP' if USE_LP_OPTIMIZER else 'Heuristic'} optimizer** | "
        f"SOC **{soc:.0f}%** | "
        f"P25 {_ctx.get('p25', 0) * 100:.1f} · "
        f"P75 {_ctx.get('p75', 0) * 100:.1f} ct/kWh  "
        f"<small>_(updated {now_str})_</small>"
    )
    md_lines.append("")
    md_lines.append("| Time | Strategy | Price | Consumption | PV forecast | Grid import | PV/Batt output | SOC end |")
    md_lines.append("|------|----------|-------|-------------|-------------|-------------|----------------|---------|")

    for w in windows:
        start_str   = w["start"].strftime("%H:%M")
        end_str     = w["end"].strftime("%H:%M")
        duration    = w["n_slots"] * 15
        desc        = label_text.get(w["label"], w["label"])
        avg_price   = _avg(w["prices"])
        min_price   = min(w["prices"])
        max_price   = max(w["prices"])
        avg_cons    = _avg(w["cons_w"])
        avg_pv      = _avg(w["pv_w"])
        avg_grid    = _avg(w["grid_w"])
        avg_batt    = _avg(w["batt_w"])
        soc_end     = w["soc_pct"][-1]
        avg_pv_batt = avg_pv + max(0.0, avg_batt)
        avg_ct      = avg_price * 100
        min_ct      = min_price * 100
        max_ct      = max_price * 100
        price_str   = f"{avg_ct:.1f} ct" if abs(min_ct - max_ct) < 0.05 else f"{avg_ct:.1f} ct ({min_ct:.1f}–{max_ct:.1f})"

        log_lines.append(
            f"  {start_str}–{end_str} ({duration:3d}min)  "
            f"{desc:<32}  {price_str:<22}  "
            f"cons {avg_cons:.0f}W  pv {avg_pv:.0f}W  "
            f"grid {avg_grid:.0f}W  pv/batt {avg_pv_batt:.0f}W  "
            f"SOC→{soc_end:.0f}%"
        )
        md_lines.append(
            f"| `{start_str}–{end_str}` ({duration}min) "
            f"| {desc} "
            f"| {price_str} "
            f"| {avg_cons:.0f} W "
            f"| {avg_pv:.0f} W "
            f"| {avg_grid:.0f} W "
            f"| {avg_pv_batt:.0f} W "
            f"| {soc_end:.0f}% |"
        )

    log_lines.append("────────────────────────────────────────────────────────────────")
    for line in log_lines:
        log.info(line)

    # ── Write Markdown file ────────────────────
    try:
        content = "\n".join(md_lines)
        with open(OUTLOOK_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Outlook written to {OUTLOOK_FILE}")
    except Exception as exc:
        log.warning(f"Could not write outlook file: {exc}")



# ════════════════════════════════════════════════════════════════════════════
# STRATEGIC LAYER — every 30 minutes
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("cron(*/30 * * * *)")
async def strategic_optimize():
    log.info(f"── Strategic cycle ({'LP' if USE_LP_OPTIMIZER else 'Heuristic'}) ──")
    try:
        soc_raw = state.get(E_BATTERY_SOC)
        if soc_raw in (None, "unavailable", "unknown"):
            log.warning("Battery SOC unavailable — skipping")
            return
        soc = float(soc_raw)

        consumption      = await _fetch_historical_consumption()
        solar            = _get_solar_forecast()
        prices           = _get_spot_prices()

        if not prices:
            log.warning("No EPEX price data — mode unchanged")
            return

        schedule         = _build_schedule(consumption, solar, prices)
        optimal_schedule = _get_schedule(soc, schedule)
        _ctx["last_schedule"] = optimal_schedule

        raw_sp = optimal_schedule[0] if optimal_schedule else 0
        net    = schedule[0]["net"]   if schedule         else 0.0
        price  = schedule[0]["price"] if schedule         else 0.15

        mode, sp = _apply_trickle_override(soc, raw_sp, net)
        _write_outputs(mode, sp)

        p25 = _ctx.get("p25", 0.10)
        p75 = _ctx.get("p75", 0.20)

        if mode == "GRID_CHARGE":
            reason = (
                f"Price {price * 100:.1f} ct/kWh is in cheapest 25% "
                f"(≤ {p25 * 100:.1f} ct). "
                f"Charging battery at max rate ({OUTPUT_MIN_W}W). SOC: {soc:.0f}%."
            )
        elif mode == "DISCHARGE":
            reason = (
                f"Price {price * 100:.1f} ct/kWh is in most expensive 25% "
                f"(≥ {p75 * 100:.1f} ct). "
                f"Discharging battery ({sp:+d}W). SOC: {soc:.0f}%."
            )
        elif mode == "TRICKLE" and soc >= BATTERY_FULL_PCT:
            reason = (
                f"Battery full ({soc:.0f}%). Holding SOC in "
                f"{BATTERY_TRICKLE_PCT}–{BATTERY_FULL_PCT}% band. "
                f"Price: {price * 100:.1f} ct/kWh."
            )
        elif mode == "TRICKLE":
            reason = (
                f"SOC {soc:.0f}% in hysteresis band "
                f"({BATTERY_TRICKLE_PCT}–{BATTERY_FULL_PCT}%). "
                f"Gently recharging. Price: {price * 100:.1f} ct/kWh."
            )
        elif mode == "BALANCE" and soc >= BATTERY_FULL_PCT and net < -GRID_DEADZONE_W:
            reason = (
                f"Battery full ({soc:.0f}%) with PV surplus {abs(net):.0f}W. "
                f"Spilling {sp:+d}W. Price: {price * 100:.1f} ct/kWh."
            )
        else:
            if USE_LP_OPTIMIZER:
                reason = (
                    f"LP: no strong signal at {price * 100:.1f} ct/kWh "
                    f"[P25={p25 * 100:.1f} P75={p75 * 100:.1f} ct]. "
                    f"Grid consumption. SOC: {soc:.0f}%. Slot-0: {raw_sp:+d}W."
                )
            else:
                fv       = _assess_future_value(schedule, p75)
                pvc      = _estimate_pv_recharge(schedule, p75)
                avail_wh = max(0.0, (soc - BATTERY_EMPTY_PCT) / 100.0 * BATTERY_SIZE_WH)
                if fv["high_demand_wh"] > 0 and avail_wh >= fv["high_demand_wh"] and pvc < fv["high_demand_wh"]:
                    reason = (
                        f"Mid price ({price * 100:.1f} ct). Holding for expensive window "
                        f"({fv['slots']} slots, {fv['high_demand_wh']:.0f}Wh). "
                        f"PV ({pvc:.0f}Wh) insufficient. SOC: {soc:.0f}%."
                    )
                elif fv["high_demand_wh"] > 0:
                    reason = (
                        f"Mid price ({price * 100:.1f} ct). Expensive window ahead. "
                        f"PV ({pvc:.0f}Wh) will recharge in time. SOC: {soc:.0f}%."
                    )
                else:
                    reason = (
                        f"Mid price ({price * 100:.1f} ct). No peak window ahead. "
                        f"Grid consumption. SOC: {soc:.0f}%."
                    )

        _update_status(mode, reason)
        log.info(
            f"Mode={mode} | SOC={soc:.0f}% | "
            f"Price={price * 100:.1f} ct | "
            f"Optimizer={raw_sp:+d}W → Applied={sp:+d}W"
        )

        now = datetime.now(TZ)
        if now.minute < 30:
            await _log_24h_outlook(schedule, optimal_schedule, soc)

    except Exception as exc:
        import traceback
        log.error(f"Strategic error: {exc}\n{traceback.format_exc()}")


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
        input_number.set_value(entity_id=E_MODE_ID,  value=0)
        input_number.set_value(entity_id=E_SETPOINT, value=0)
        _update_status(
            "BALANCE",
            f"⚠️ Emergency: SOC critically low ({soc_raw}%). "
            f"Inverter forced to 0W."
        )
        persistent_notification.create(
            title="⚠️ Battery Critical",
            message=f"SOC is {soc_raw}% — inverter forced to 0 W.",
            notification_id="energy_optimizer_critical",
        )
        log.warning(f"Battery critical ({soc_raw}%) — forced BALANCE, setpoint 0W")


# ════════════════════════════════════════════════════════════════════════════
# MANUAL SERVICE CALL
# ════════════════════════════════════════════════════════════════════════════

@service
async def energy_optimizer_force_run():
    """Callable via Developer Tools → Actions → pyscript.energy_optimizer_force_run"""
    log.info("Manual trigger — running strategic cycle now")
    await strategic_optimize()
