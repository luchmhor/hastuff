#!/usr/bin/env python3
"""
energy_optimizer_backtest.py

Reads a CSV with 15-min slots and runs the LP optimizer offline.

CSV format (header row required):
  timestamp,consumption_w,pv_w,price_eur_per_kwh
  2026-03-02 00:00,120,0,0.106
  ...

Usage:
  python energy_optimizer_backtest.py input.csv --soc 50 --output results.csv
"""

import argparse
import csv
import sys
from datetime import datetime, timedelta

from scipy.optimize import linprog

# ── Configuration (mirror of pyscript constants) ─────────────────────────────

BATTERY_SIZE_WH     = 2760
OUTPUT_MIN_W        = -1200
OUTPUT_MAX_W        =  1200
BATTERY_FULL_PCT    =  98
BATTERY_EMPTY_PCT   =  15
GRID_DEADZONE_W     =  10
DISCHARGE_PENALTY   =  0.0001
ALLOW_EXPORT        =  False

# ── LP Optimizer ─────────────────────────────────────────────────────────────

def solve_optimal_schedule(soc: float, loads: list, solars: list, prices: list) -> list:
    N     = len(loads)
    DT    = 0.25
    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH

    c_obj = []
    for t in range(N):
        c_obj.append(-prices[t] * DT / 1000.0 + DISCHARGE_PENALTY * DT / 1000.0)
    for t in range(N):
        c_obj.append(prices[t] * DT / 1000.0)

    bounds = []
    for t in range(N):
        bounds.append((float(OUTPUT_MIN_W), float(OUTPUT_MAX_W)))
    for t in range(N):
        bounds.append((0.0, None))

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

    result = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds, method="highs")
    if result.status != 0:
        print(f"  WARNING: LP status {result.status}: {result.message}", file=sys.stderr)
        return [0] * N

    optimal = []
    for t in range(N):
        sp = int(round(result.x[t] / 10) * 10)
        sp = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, sp))
        optimal.append(sp)
    return optimal


# ── CSV I/O ───────────────────────────────────────────────────────────────────

def load_csv(path: str) -> list:
    slots = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            ts = None
            if "timestamp" in row and row["timestamp"].strip():
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                except ValueError:
                    pass
            if ts is None:
                ts = datetime(2000, 1, 1) + timedelta(minutes=15 * i)
            slots.append({
                "timestamp": ts,
                "cons":      float(row["consumption_w"]),
                "solar":     float(row["pv_w"]),
                "price":     float(row["price_eur_per_kwh"]),
            })
    return slots


def write_csv(path: str, rows: list):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


# ── Strategy label ────────────────────────────────────────────────────────────

def strategy_label(sp: int, price: float, net: float, p75: float) -> str:
    if sp <= -GRID_DEADZONE_W:
        return "GRID_CHARGE"
    elif sp >= GRID_DEADZONE_W and price >= p75:
        return "DISCHARGE_PEAK"
    elif sp >= GRID_DEADZONE_W and price < p75:
        return "COVER_LOAD"
    elif net < -GRID_DEADZONE_W:
        return "PV_SURPLUS"
    else:
        return "GRID_CONSUMPTION"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Energy optimizer backtest")
    parser.add_argument("input",          help="Input CSV file")
    parser.add_argument("--soc",          type=float, default=50.0,
                        help="Initial battery SOC %% (default: 50)")
    parser.add_argument("--output",       default="results.csv",
                        help="Output CSV file (default: results.csv)")
    parser.add_argument("--rolling",      action="store_true",
                        help="Re-optimize every slot with updated SOC (slower, more realistic)")
    args = parser.parse_args()

    slots = load_csv(args.input)
    if not slots:
        print("ERROR: No data in input CSV", file=sys.stderr)
        sys.exit(1)

    N      = len(slots)
    loads  = [s["cons"]  for s in slots]
    solars = [s["solar"] for s in slots]
    prices = [s["price"] for s in slots]

    # Percentile thresholds for labelling
    sorted_prices = sorted(prices)
    p25 = sorted_prices[max(0, int(N * 0.25) - 1)]
    p75 = sorted_prices[min(N - 1, int(N * 0.75))]
    print(f"Slots: {N} | P25={p25*100:.1f} ct | P75={p75*100:.1f} ct | Initial SOC={args.soc:.0f}%")

    DT    = 0.25
    E_now = args.soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH

    if args.rolling:
        # Re-optimize every slot with remaining horizon
        print("Mode: rolling re-optimization")
        setpoints = []
        e = E_now
        for i in range(N):
            soc_i   = e / BATTERY_SIZE_WH * 100.0
            horizon = solve_optimal_schedule(
                soc_i,
                loads[i:], solars[i:], prices[i:]
            )
            setpoints.append(horizon[0])
            sp = horizon[0]
            e -= sp * DT
            e  = max(E_min, min(E_max, e))
    else:
        # Single solve over full horizon
        print("Mode: single full-horizon solve")
        setpoints = solve_optimal_schedule(args.soc, loads, solars, prices)

    # Simulate and build output rows
    output_rows = []
    e           = E_now
    total_grid_cost   = 0.0
    total_grid_kwh    = 0.0
    total_charge_kwh  = 0.0
    total_discharge_kwh = 0.0

    for i, s in enumerate(slots):
        sp    = setpoints[i]
        net   = s["cons"] - s["solar"]
        price = s["price"]

        # Battery energy update
        e_before = e
        e       -= sp * DT
        e        = max(E_min, min(E_max, e))
        soc_end  = e / BATTERY_SIZE_WH * 100.0

        # Actual grid import (positive = import, negative = export)
        grid_w   = s["cons"] - s["solar"] - sp
        grid_w   = max(0.0, grid_w) if not ALLOW_EXPORT else grid_w

        # Cost
        grid_kwh  = grid_w * DT / 1000.0
        slot_cost = grid_kwh * price if grid_w > 0 else 0.0
        total_grid_cost  += slot_cost
        total_grid_kwh   += max(0.0, grid_kwh)
        if sp > 0:
            total_discharge_kwh += sp * DT / 1000.0
        elif sp < 0:
            total_charge_kwh    += abs(sp) * DT / 1000.0

        label = strategy_label(sp, price, net, p75)

        output_rows.append({
            "timestamp":        s["timestamp"].strftime("%Y-%m-%d %H:%M"),
            "consumption_w":    round(s["cons"],  1),
            "pv_w":             round(s["solar"], 1),
            "price_ct_per_kwh": round(price * 100, 3),
            "setpoint_w":       sp,
            "grid_import_w":    round(grid_w, 1),
            "soc_start_pct":    round(e_before / BATTERY_SIZE_WH * 100.0, 1),
            "soc_end_pct":      round(soc_end, 1),
            "slot_cost_eur":    round(slot_cost, 5),
            "strategy":         label,
        })

    write_csv(args.output, output_rows)

    # ── Summary ───────────────────────────────────────────────────────────────
    naive_cost = sum(
        max(0.0, (loads[i] - solars[i]) * DT / 1000.0) * prices[i]
        for i in range(N)
    )
    print(f"\n{'─'*55}")
    print(f"  Grid import:      {total_grid_kwh:.2f} kWh")
    print(f"  Grid cost:        {total_grid_cost:.4f} €")
    print(f"  Naive cost:       {naive_cost:.4f} €  (no battery)")
    print(f"  Savings:          {naive_cost - total_grid_cost:.4f} €  "
          f"({(1 - total_grid_cost/naive_cost)*100:.1f}%)" if naive_cost > 0 else "")
    print(f"  Charged:          {total_charge_kwh:.2f} kWh")
    print(f"  Discharged:       {total_discharge_kwh:.2f} kWh")
    print(f"  Final SOC:        {output_rows[-1]['soc_end_pct']:.1f}%")
    print(f"{'─'*55}")
    print(f"  Results written → {args.output}")


if __name__ == "__main__":
    main()
