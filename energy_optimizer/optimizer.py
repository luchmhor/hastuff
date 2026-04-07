# optimizer.py
"""
Core optimization – schedule building, LP solver, heuristic fallback,
SOC‑based overrides.
All numeric constants come from CONFIG.
"""

from scipy.optimize import linprog
import math
from ._config import CONFIG
from datetime import datetime, timedelta
import pytz

# ----------------------------------------------------------------------
# Helper to build the 24‑h schedule (used by both LP and heuristic)
# ----------------------------------------------------------------------
def build_schedule(consumption: dict, solar: dict, prices: dict) -> list:
    now = datetime.now(pytz.timezone(CONFIG.general.timezone)).replace(
        minute=(datetime.now(pytz.timezone(CONFIG.general.timezone)).minute // 15) * 15,
        second=0, microsecond=0,
    )
    schedule = []
    for i in range(CONFIG.optimization.schedule_slots):
        t = now + timedelta(minutes=15 * i)
        key = (t.hour, t.minute // 15)
        schedule.append({
            "i": i,
            "time": t,
            "cons": consumption.get(key, 300.0),
            "solar": solar.get(t.hour, 0.0),
            "price": prices.get(key, 0.15),
            "net": consumption.get(key, 300.0) - solar.get(t.hour, 0.0),
        })
    return schedule


# ----------------------------------------------------------------------
# LP solver
# ----------------------------------------------------------------------
def _solve_optimal_schedule(soc: float, schedule: list) -> list:
    N = len(schedule)
    DT = 0.25   # h per slot
    E_now   = soc / 100.0 * CONFIG.battery.size_wh
    E_min   = CONFIG.battery.empty_pct / 100.0 * CONFIG.battery.size_wh
    E_max   = CONFIG.battery.full_pct  / 100.0 * CONFIG.battery.size_wh

    loads  = [s["cons"]  for s in schedule]
    solars = [s["solar"] for s in schedule]
    prices = [s["price"] for s in schedule]

    pv_threshold = CONFIG.solar.pv_threshold_w
    surplus = [min(max(0.0, solars[t] - loads[t]), abs(CONFIG.battery.output_min_w))
               for t in range(N)]

    price_max, price_min = max(prices), min(prices)
    price_avg = sum(prices) / N

    # ---- look‑ahead PV surplus (Wh) ----
    expected_pv_surplus_wh = sum([
        min(max(0.0, solars[t] - loads[t]), abs(CONFIG.battery.output_min_w)) *
        DT * CONFIG.battery.charge_efficiency
        for t in range(N)
    ])
    pv_will_fill_battery = (E_now + expected_pv_surplus_wh) >= E_max
    pv_adjusted_headroom = max(0.0, E_max - E_now - expected_pv_surplus_wh)

    # ---- objective (2N variables: b[t] discharge, g[t] charge) ----
    c_obj = []
    for t in range(N):
        opp = (price_max - prices[t]) * CONFIG.battery.discharge_efficiency * DT / 1000.0 * CONFIG.optimization.opportunity_cost_weight
        c_obj.append(
            -prices[t] * CONFIG.battery.discharge_efficiency * DT / 1000.0
            + opp
            + CONFIG.optimization.discharge_penalty * DT / 1000.0
        )
    for t in range(N):
        pv_headroom_ratio = min(1.0, pv_adjusted_headroom / max(1.0, CONFIG.battery.size_wh * 0.3))
        cheap = max(0.0, price_avg - prices[t]) * CONFIG.battery.charge_efficiency * DT / 1000.0 * CONFIG.optimization.opportunity_cost_weight * pv_headroom_ratio
        c_obj.append(prices[t] * DT / 1000.0 - cheap)

    # ---- bounds ----
    bounds = []
    p25 = 0.10   # will be overwritten by caller via _ctx later; keep placeholder
    for t in range(N):
        # discharge max
        if soc >= CONFIG.battery.full_pct and solars[t] >= pv_threshold:
            max_disch = float(CONFIG.battery.output_max_w)
        elif solars[t] >= pv_threshold:
            max_disch = max(0.0, loads[t])
        else:
            max_disch = float(CONFIG.battery.output_max_w)

        # charge min (negative)
        if soc >= CONFIG.battery.full_pct or pv_will_fill_battery:
            min_ch = 0.0
        elif soc >= CONFIG.optimization.grid_charge_soc_cheap_pct:
            min_ch = float(CONFIG.battery.output_min_w) if prices[t] <= p25 else 0.0
        else:
            min_ch = float(CONFIG.battery.output_min_w)

        bounds.append((min_ch, max_disch))   # b[t]
        bounds.append((0.0, None))           # g[t] ≥ 0

    # ---- inequality constraints A_ub·x ≤ b_ub ----
    A_ub, b_ub = [], []

    # 1) grid slack
    for t in range(N):
        row = [0.0] * (2 * N)
        row[t] = -1.0          # b[t]
        row[N + t] = -1.0      # g[t]
        A_ub.append(row)
        b_ub.append(solars[t] - loads[t])

    # 2+3) SOC bounds (simple cumulative formulation)
    pv_slots = [t for t in range(N) if solars[t] >= pv_threshold]
    last_pv = pv_slots[-1] if pv_slots else -1
    soc_weight = min(1.0, (E_now - E_min) / max(1.0, E_max - E_min))
    est_dis = sum([
        max(0.0, loads[t]) * DT
        for t in range(N)
        if t > last_pv and solars[t] < pv_threshold
    ]) * soc_weight
    headroom = min(
        E_max - E_now + est_dis * CONFIG.battery.discharge_efficiency,
        CONFIG.battery.size_wh * (CONFIG.battery.full_pct - CONFIG.battery.empty_pct) / 100.0,
    )
    cum = 0.0
    for k in range(N):
        absorbed = min(surplus[k] * DT * CONFIG.battery.charge_efficiency, headroom)
        headroom = max(0.0, headroom - absorbed)
        cum += absorbed

        # lower SOC: E_now - E_min + Σ(b*DT/η_dis) - Σ(g*DT*η_ch) ≥ 0
        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = DT / CONFIG.battery.discharge_efficiency
        A_ub.append(row)
        b_ub.append(E_now - E_min + cum)

        # upper SOC: E_max - E_now - Σ(b*DT/η_dis) + Σ(g*DT*η_ch) ≥ 0
        row = [0.0] * (2 * N)
        for t in range(k + 1):
            row[t] = -DT * CONFIG.battery.charge_efficiency
        A_ub.append(row)
        b_ub.append(max(0.0, E_max - E_now - cum))

    # 4) no‑export
    if not CONFIG.grid.allow_export:
        for t in range(N):
            net = loads[t] - solars[t]
            if net >= 0:
                row = [0.0] * (2 * N)
                row[t] = 1.0          # b[t] only (discharge) reduces export
                A_ub.append(row)
                b_ub.append(net)

    # ---- solve ----
    try:
        res = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
        if res.status != 0:
            raise RuntimeError(f"LP status {res.status}: {res.message}")

        optimal = []
        e = E_now
        for t in range(N):
            sp = int(round(res.x[t] / 10) * 10)
            sp = max(CONFIG.battery.output_min_w, min(CONFIG.battery.output_max_w, sp))
            optimal.append(sp)
            if sp > 0:                     # discharging
                e -= sp * DT / CONFIG.battery.discharge_efficiency
            else:                          # charging (negative)
                e -= sp * DT * CONFIG.battery.charge_efficiency
            e += surplus[t] * DT * CONFIG.battery.charge_efficiency
            e = max(E_min, min(E_max, e))
        return optimal
    except Exception:
        # fallback to heuristic (see below)
        return _heuristic_schedule(soc, schedule)


# ----------------------------------------------------------------------
# Heuristic fallback
# ----------------------------------------------------------------------
def _heuristic_schedule(soc: float, schedule: list) -> list:
    prices = [s["price"] for s in schedule]
    prices_sorted = sorted(prices)
    n = len(prices_sorted)
    p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
    p75 = prices_sorted[min(n - 1, int(n * 0.75))]

    available_wh = max(0.0, (soc - CONFIG.battery.empty_pct) / 100.0 * CONFIG.battery.size_wh)
    high_demand_wh = sum(
        max(0.0, s["net"]) * 0.25
        for s in schedule[1:] if s["price"] >= p75 and s["net"] > 0
    )
    pv_recharge_wh = sum(
        abs(s["net"]) * 0.25
        for s in schedule[1:] if s["net"] < 0
    )

    result = []
    for s in schedule:
        price = s["price"]
        net   = s["net"]
        net_load = max(0, int(net))

        if soc <= CONFIG.battery.empty_pct:
            sp = CONFIG.battery.output_min_w if price <= p25 else 0
        elif price >= p75:
            sp = min(CONFIG.battery.output_max_w, max(0, int(net)))
        elif price <= p25:
            if soc < CONFIG.battery.full_pct:   # allow charging only if not already full
                sp = CONFIG.battery.output_min_w
            else:
                sp = 0
        else:
            if high_demand_wh > 0 and available_wh >= high_demand_wh and pv_recharge_wh < high_demand_wh:
                sp = min(int(net), net_load)
            elif high_demand_wh > 0:
                sp = 0
            else:
                sp = min(int(net), net_load)
        result.append(sp)
    return result


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def get_optimal_setpoints(soc: float, schedule: list, use_lp: bool = True) -> list:
    """Return list of setpoint values (W) for each 15‑min slot."""
    if use_lp:
        return _solve_optimal_schedule(soc, schedule)
    return _heuristic_schedule(soc, schedule)


def apply_trickle_override(soc: float, raw_sp: int, net: float, price: float,
                           p25: float, p75: float) -> tuple:
    """
    Return (mode, final_setpoint) after applying SOC‑based overrides.
    Mode strings: "GRID_CHARGE", "DISCHARGE", "BALANCE", "TRICKLE".
    """
    # 1) full battery → anti‑curtail discharge
    if soc >= CONFIG.battery.full_pct:
        pv_surplus = max(0.0, -net)          # net = cons - solar → surplus = solar - cons
        if pv_surplus > CONFIG.grid.grid_deadzone_w:
            sp_anti = min(int(round(pv_surplus / 10) * 10), CONFIG.battery.output_max_w)
            return ("DISCHARGE", sp_anti)
        # no surplus → honour LP if it wants to discharge, else idle
        if raw_sp > CONFIG.grid.grid_deadzone_w:
            return ("DISCHARGE", raw_sp)
        return ("TRICKLE", 0)

    # 2) trickle band
    if soc >= CONFIG.battery.trickle_pct:
        if net < -CONFIG.grid.grid_deadzone_w:
            return ("BALANCE", 0)
        return ("TRICKLE", -CONFIG.battery.trickle_w)

    # 3) suppress grid charge at high SOC
    if raw_sp < -CONFIG.grid.grid_deadzone_w:
        if soc >= CONFIG.battery.full_pct:
            return ("BALANCE", 0)
        if soc >= CONFIG.optimization.grid_charge_soc_block_pct:
            return ("BALANCE", 0)
        if (soc >= CONFIG.optimization.grid_charge_soc_cheap_pct and
                price > p25):
            return ("BALANCE", 0)

    # 4) honour discharge when price is expensive
    if price >= p75:
        return ("DISCHARGE", raw_sp)

    # 5) default mapping
    if raw_sp < -CONFIG.grid.grid_deadzone_w:
        mode = "GRID_CHARGE"
    elif raw_sp > CONFIG.grid.grid_deadzone_w:
        mode = "DISCHARGE"
    else:
        mode = "BALANCE"
    return (mode, raw_sp)
