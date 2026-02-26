# pyscript/energy_optimizer.py
"""
Energy Optimizer — pyscript (HACS)

Two-layer control:
  Strategic (15 min): InfluxDB history + Solcast + EPEX → operating MODE + guidance setpoint
  Tactical   (5 sec): real-time proportional controller targeting grid_power ≈ 0

Additional triggers:
  - Re-runs strategic cycle immediately when EPEX prices update (~17:00)
  - Emergency guard: forces inverter to 0 W if SOC drops below hard floor

Requires in configuration.yaml:
  pyscript:
    allow_all_imports: true

File structure:
  /config/pyscript/energy_optimizer.py   ← this file
  /config/pyscript/modules/influx_helper.py
"""

import pytz
from datetime import datetime, timedelta

from modules.influx_helper import fetch_historical_consumption

# ── Hardware constants ────────────────────────────────────────────────────
BATTERY_SIZE_WH   = 2760
OUTPUT_MIN_W      = -1200    # negative = grid charging battery
OUTPUT_MAX_W      =  1200    # positive = discharge to home / export to grid
BATTERY_FULL_PCT  =  98
BATTERY_EMPTY_PCT =  15
GRID_DEADZONE_W   =  10

# ── Real-time proportional controller tuning ─────────────────────────────
RT_GAIN      = 0.8   # fraction of grid_power error applied per 5-sec step
RT_ROUND_W   = 10    # round setpoint to nearest N watts to reduce chatter
RT_MIN_DELTA = 10    # minimum W change before writing to HA (avoids spam)

# ── HA entity IDs ─────────────────────────────────────────────────────────
E_GRID_POWER     = "sensor.shrdzm_485519e15aae_16_7_0"
E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER  = "sensor.ezhi_battery_power"
E_INV_OUTPUT     = "number.apsystems_ezhi_max_output_power"
E_PRICE_DATA     = "sensor.epex_spot_data_price"
E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

# ── Shared context between strategic and tactical layers ──────────────────
_ctx = {
    "mode":         "BALANCE",   # GRID_CHARGE | DISCHARGE | EXPORT | BALANCE
    "setpoint":     0,           # last setpoint written to HA (W)
    "strategic_sp": 0,           # guidance setpoint from strategic layer
    "price":        0.15,        # current 15-min slot price
    "p25":          0.10,        # lower price percentile (24 h horizon)
    "p75":          0.20,        # upper price percentile (24 h horizon)
    "last_soc":     50.0,        # cached SOC for tactical layer fallback
}

TZ = pytz.timezone("Europe/Vienna")


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _get_solar_forecast() -> dict:
    """
    Returns {hour: mean_watts} from Solcast sensor attributes.
    pv_estimate is mean kW over a 30-min period; two 30-min blocks accumulate
    per hour → divide by 4 at schedule-build time to get average W per 15-min slot.
    """
    solar = {}
    try:
        attrs = state.getattr(E_SOLAR_HOUR) or {}
        fl    = (
            attrs.get("forecast") or
            attrs.get("detailedForecast") or
            attrs.get("DetailedForecast") or
            attrs.get("forecasts") or
            []
        )
        for entry in fl:
            t_str = entry.get("period_start") or entry.get("PeriodStart")
            pv_kw = float(entry.get("pv_estimate") or entry.get("PvEstimate") or 0)
            if t_str:
                t = datetime.fromisoformat(t_str).astimezone(TZ)
                solar[t.hour] = solar.get(t.hour, 0.0) + pv_kw * 1000
        if not solar:
            # Scalar fallback: sensor state = Wh for the next hour = average W for 60 min
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
        for entry in data:
            t = datetime.fromisoformat(entry["start_time"]).astimezone(TZ)
            prices[(t.hour, t.minute // 15)] = float(entry["price_per_kwh"])
    except Exception as exc:
        log.error(f"Spot price error: {exc}")
    return prices


def _build_schedule(consumption: dict, solar: dict, prices: dict) -> list:
    """
    Assembles 96 forward-looking 15-min slots from the current timestamp.
    net_load > 0 → apartment needs power from battery/grid
    net_load < 0 → solar surplus, can charge battery or export
    """
    now = datetime.now(TZ)
    out = []
    for i in range(96):
        t   = now + timedelta(minutes=15 * i)
        key = (t.hour, t.minute // 15)
        c   = consumption.get(key, 300.0)
        s   = solar.get(t.hour, 0.0) / 4.0   # hourly W → 15-min average W
        p   = prices.get(key, 0.15)
        out.append({
            "i": i, "time": t,
            "cons": c, "solar": s, "price": p,
            "net": c - s,
        })
    return out


def _compute_mode(soc: float, schedule: list) -> tuple:
    """
    Determines operating mode and strategic setpoint from price percentiles.
    Returns (mode_str, setpoint_W, p25, p75).

    Modes:
      GRID_CHARGE  — draw from grid to charge battery (cheap period or SOC critical)
      DISCHARGE    — maximize battery discharge (expensive period)
      EXPORT       — max output when battery full and price positive
      BALANCE      — proportional self-consumption (tactical layer takes over)
    """
    if not schedule:
        return "BALANCE", 0, 0.10, 0.20

    prices_sorted = sorted(s["price"] for s in schedule)
    n   = len(prices_sorted)
    p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
    p75 = prices_sorted[min(n - 1, int(n * 0.75))]

    price = schedule[0]["price"]
    net   = schedule[0]["net"]

    if soc <= BATTERY_EMPTY_PCT:
        return ("GRID_CHARGE", OUTPUT_MIN_W, p25, p75) if price <= p25 \
               else ("BALANCE", 0, p25, p75)
    elif soc >= BATTERY_FULL_PCT:
        return ("EXPORT", OUTPUT_MAX_W, p25, p75) if price > 0 \
               else ("BALANCE", max(0, int(net)), p25, p75)
    elif price >= p75:
        return "DISCHARGE", OUTPUT_MAX_W, p25, p75
    elif price <= p25:
        return "GRID_CHARGE", OUTPUT_MIN_W, p25, p75
    else:
        return "BALANCE", int(net), p25, p75


def _apply_if_changed(new_sp: int):
    """Write setpoint to HA only when it differs by ≥ RT_MIN_DELTA W."""
    new_sp  = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, new_sp))
    current = _ctx["setpoint"]
    if abs(new_sp - current) >= RT_MIN_DELTA:
        number.set_value(entity_id=E_INV_OUTPUT, value=new_sp)
        _ctx["setpoint"] = new_sp
        log.debug(f"⚡ inverter {current:+d} W → {new_sp:+d} W")


# ════════════════════════════════════════════════════════════════════════════
# STRATEGIC LAYER — every 15 minutes
# Queries InfluxDB + sensors → determines MODE and guidance setpoint
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("period(now, 15min)")
def strategic_optimize():
    log.info("── Strategic optimization cycle ──")
    try:
        soc_raw = state.get(E_BATTERY_SOC)
        if soc_raw in (None, "unavailable", "unknown"):
            log.warning("Battery SOC unavailable — skipping strategic cycle")
            return
        soc = float(soc_raw)
        _ctx["last_soc"] = soc

        # fetch_historical_consumption is a plain Python function in
        # modules/influx_helper.py — safe to call via task.executor()
        consumption = task.executor(fetch_historical_consumption)
        solar       = _get_solar_forecast()
        prices      = _get_spot_prices()

        if not prices:
            log.warning("No EPEX price data available — mode unchanged")
            return

        schedule             = _build_schedule(consumption, solar, prices)
        mode, sp, p25, p75   = _compute_mode(soc, schedule)

        _ctx["mode"]         = mode
        _ctx["strategic_sp"] = sp
        _ctx["p25"]          = p25
        _ctx["p75"]          = p75
        _ctx["price"]        = schedule[0]["price"] if schedule else 0.15

        # For locked modes apply the setpoint immediately;
        # BALANCE leaves fine-tuning to the 5-sec tactical layer.
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
# Real-time proportional control targeting grid_power ≈ 0
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("period(now, 5s)")
def realtime_control():
    """
    Every 5 seconds:

    GRID_CHARGE / DISCHARGE / EXPORT:
      Re-applies the strategic setpoint each cycle (guards against inverter resets).

    BALANCE (self-consumption):
      Proportional controller: new_sp = current_sp + GAIN × grid_power
        grid_power > 0  →  importing  →  increase output (discharge more / charge less)
        grid_power < 0  →  exporting  →  decrease output (charge more / export less)
      Setpoint is rounded to 10 W and only written to HA if change ≥ RT_MIN_DELTA W.
    """
    try:
        mode = _ctx["mode"]

        # ── Non-BALANCE: re-assert strategic setpoint ─────────────────────
        if mode != "BALANCE":
            _apply_if_changed(_ctx["strategic_sp"])
            return

        # ── BALANCE: proportional real-time control ───────────────────────
        grid_raw = state.get(E_GRID_POWER)
        if grid_raw in (None, "unavailable", "unknown"):
            return
        grid = float(grid_raw)   # W  (positive = importing, negative = exporting)

        soc_raw = state.get(E_BATTERY_SOC)
        soc     = float(soc_raw) if soc_raw not in (None, "unavailable", "unknown") \
                  else _ctx["last_soc"]
        _ctx["last_soc"] = soc

        # Dead-zone: skip if grid is already balanced
        if abs(grid) <= GRID_DEADZONE_W:
            return

        current_sp = _ctx["setpoint"]
        raw_sp     = current_sp + RT_GAIN * grid
        new_sp     = int(round(raw_sp / RT_ROUND_W) * RT_ROUND_W)

        # Battery SOC safety guards
        if new_sp > current_sp and soc <= BATTERY_EMPTY_PCT:
            return   # cannot discharge further
        if new_sp < current_sp and soc >= BATTERY_FULL_PCT:
            return   # cannot charge further

        _apply_if_changed(new_sp)

    except Exception as exc:
        log.error(f"Realtime control error: {exc}")


# ════════════════════════════════════════════════════════════════════════════
# EVENT TRIGGERS
# ════════════════════════════════════════════════════════════════════════════

@state_trigger(E_PRICE_DATA)
def on_price_update(**kwargs):
    """Fires whenever the EPEX sensor changes (new prices published ~17:00)."""
    log.info("EPEX price data updated — triggering strategic cycle")
    strategic_optimize()


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
def energy_optimizer_force_run():
    """Callable via Developer Tools → Actions → pyscript.energy_optimizer_force_run"""
    log.info("Manual trigger — running strategic cycle now")
    strategic_optimize()
