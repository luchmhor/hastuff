# output_handler.py
"""
All side‑effects: writing to HA entities, creating outlook files,
pushing forecast to InfluxDB.
Configuration values are taken from CONFIG.
"""

import os
import csv
from datetime import datetime, timedelta
from ._config import CONFIG

# ----------------------------------------------------------------------
# HA output helpers (these are thin wrappers – in pyscript you call the
# actual HA services; here we just print when LOG_DEBUG is true)
# ----------------------------------------------------------------------
def write_ha_outputs(mode: str, setpoint: int):
    mode_id = {"BALANCE":0, "GRID_CHARGE":1, "DISCHARGE":2, "TRICKLE":3}.get(mode, 0)
    if CONFIG.general.log_debug:
        print(f"[HA] mode_id={mode_id} ({mode}) setpoint={setpoint:+d}W")
    # In pyscript replace the print with:
    # input_number.set_value(entity_id="input_number.energy_optimizer_mode_id", value=mode_id)
    # input_number.set_value(entity_id="input_number.energy_optimizer_setpoint", value=setpoint)


def update_status(mode: str, reason: str):
    icons = {
        "GRID_CHARGE":"⚡ GRID CHARGE",
        "DISCHARGE":  "🔋 DISCHARGE",
        "BALANCE":    "🏭 GRID CONSUMPTION",
        "TRICKLE":    "🌿 TRICKLE",
    }
    label = icons.get(mode, mode)
    if CONFIG.general.log_debug:
        print(f"[STATUS] {label} | {reason}")
    # In pyscript:
    # input_text.set_value(entity_id="input_text.energy_optimizer_mode",   value=label)
    # input_text.set_value(entity_id="input_text.energy_optimizer_reason", value=reason)


# ----------------------------------------------------------------------
# 24‑h outlook & logging
# ----------------------------------------------------------------------
def log_24h_outlook(schedule, optimal_schedule, soc: float, use_lp: bool):
    """Create markdown outlook, CSV forecast, and InfluxDB points."""
    if not schedule or not optimal_schedule:
        return

    now = datetime.now(pytz.timezone(CONFIG.general.timezone))
    p25 = 0.10   # in a full version you would receive these from the optimizer’s context
    p75 = 0.20
    DT = 0.25
    E_now   = soc / 100.0 * CONFIG.battery.size_wh
    E_min   = CONFIG.battery.empty_pct / 100.0 * CONFIG.battery.size_wh
    E_max   = CONFIG.battery.full_pct  / 100.0 * CONFIG.battery.size_wh

    # ---- build slot dicts (same logic as original script) ----
    slots = []
    e = E_now
    for i in range(min(len(schedule), len(optimal_schedule))):
        s   = schedule[i]
        sp  = optimal_schedule[i]
        p   = s["price"]
        n   = s["net"]
        pv_surplus = min(max(0.0, s["solar"] - s["cons"]), abs(CONFIG.battery.output_min_w))

        soc_start = e / CONFIG.battery.size_wh * 100.0
        if sp > 0:
            e_after = e - sp * DT / CONFIG.battery.discharge_efficiency
        else:
            e_after = e - sp * DT * CONFIG.battery.charge_efficiency
        e_after += pv_surplus * DT * CONFIG.battery.charge_efficiency
        e_after = max(E_min, min(E_max, e_after))
        soc_after = e_after / CONFIG.battery.size_wh * 100.0

        if sp <= -CONFIG.grid.grid_deadzone_w:
            label = "PV_CHARGE" if s["solar"] - s["cons"] > abs(sp)*0.8 else "GRID_CHARGE"
        elif sp >= CONFIG.grid.grid_deadzone_w and p >= p75:
            label = "COVER_LOAD_PEAK"
        elif sp >= CONFIG.grid.grid_deadzone_w and p < p75:
            label = "COVER_LOAD"
        elif n < -CONFIG.grid.grid_deadzone_w:
            label = "PV_SURPLUS"
        else:
            label = "GRID_CONSUMPTION"

        grid_w = s["cons"] - s["solar"] - sp
        if not CONFIG.grid.allow_export:
            grid_w = max(0.0, grid_w)

        slots.append({
            "time": s["time"],
            "label": label,
            "price": p,
            "cons_w": s["cons"],
            "pv_w": s["solar"],
            "batt_w": sp,
            "grid_w": grid_w,
            "soc_start_pct": round(soc_start,1),
            "soc_pct": soc_after,
        })
        e = e_after

    # ---- window aggregation (identical to original) ----
    def _new_window(slot):
        return {
            "label": slot["label"],
            "start": slot["time"],
            "prices": [slot["price"]],
            "cons_w": [slot["cons_w"]],
            "pv_w": [slot["pv_w"]],
            "batt_w": [slot["batt_w"]],
            "grid_w": [slot["grid_w"]],
            "soc_pct": [slot["soc_pct"]],
            "n_slots": 1,
        }

    windows = []
    cur = _new_window(slots[0])
    for slot in slots[1:]:
        if slot["label"] == cur["label"]:
            for k in ["prices","cons_w","pv_w","batt_w","grid_w","soc_pct"]:
                cur[k].append(slot[k])
            cur["n_slots"] += 1
        else:
            cur["end"] = slot["time"]
            windows.append(cur)
            cur = _new_window(slot)
    cur["end"] = cur["start"] + timedelta(minutes=15 * cur["n_slots"])
    windows.append(cur)

    # ---- markdown ----
    now_str = now.strftime("%d.%m.%Y %H:%M")
    md_lines = [
        f"**{'LP' if use_lp else 'Heuristic'} optimizer** | "
        f"SOC **{soc:.0f}%** | "
        f"P25 {p25*100:.1f} · P75 {p75*100:.1f} ct/kWh _(updated {now_str})_",
        "",
        "| Time | Strategy | Price | Consumption | PV forecast | Grid import | Batt setpoint | SOC end |",
        "|------|----------|-------|-------------|-------------|-------------|---------------|---------|",
    ]
    for w in windows:
        start = w["start"].strftime("%H:%M")
        end   = w["end"].strftime("%H:%M")
        dur   = w["n_slots"] * 15
        desc  = {
            "GRID_CHARGE":"⚡ Charge from grid",
            "PV_CHARGE":"☀️ Charge from PV",
            "COVER_LOAD_PEAK":"🔋 Cover load (peak price)",
            "COVER_LOAD":"⚖️ Cover load",
            "PV_SURPLUS":"🌤️ PV surplus / spill",
            "GRID_CONSUMPTION":"🏭 Grid consumption",
        }.get(w["label"], w["label"])
        avg_price = sum(w["prices"])/len(w["prices"])
        price_str = f"{avg_price*100:.1f} ct"
        md_lines.append(
            f"| `{start}–{end}` ({dur}min) "
            f"| {desc} "
            f"| {price_str} "
            f"| {sum(w['cons_w'])/len(w['cons_w']):.0f} W "
            f"| {sum(w['pv_w'])/len(w['pv_w']):.0f} W "
            f"| {sum(w['grid_w'])/len(w['grid_w']):.0f} W "
            f"| {sum(w['batt_w'])/len(w['batt_w']):.0f} W "
            f"| {w['soc_pct'][-1]:.0f}% |"
        )
    md_lines.append("────────────────────────────────────────────────────────────────")
    md_content = "\n".join(md_lines)

    try:
        with os.fdopen(os.open(CONFIG.files.outlook_md,
                               os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644),
                       "w", encoding="utf-8") as f:
            f.write(md_content)
        if CONFIG.general.log_debug:
            print(f"[OUTLOOK] written to {CONFIG.files.outlook_md}")
    except Exception as e:
        if CONFIG.general.log_debug:
            print(f"[OUTLOOK] write error: {e}")

    # ---- CSV ----
    try:
        with os.fdopen(os.open(CONFIG.files.forecast_csv,
                               os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o644),
                       "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "time","label","price_ct","cons_w","pv_w","batt_w","grid_w",
                "soc_start_pct","soc_end_pct"
            ])
            writer.writeheader()
            for s in slots:
                writer.writerow({
                    "time": s["time"].strftime("%Y-%m-%dT%H:%M"),
                    "label": s["label"],
                    "price_ct": round(s["price"]*100,3),
                    "cons_w": round(s["cons_w"],1),
                    "pv_w": round(s["pv_w"],1),
                    "batt_w": s["batt_w"],
                    "grid_w": round(s["grid_w"],1),
                    "soc_start_pct": s["soc_start_pct"],
                    "soc_end_pct": round(s["soc_pct"],1),
                })
        if CONFIG.general.log_debug:
            print(f"[CSV] written to {CONFIG.files.forecast_csv}")
    except Exception as e:
        if CONFIG.general.log_debug:
            print(f"[CSV] write error: {e}")

    # ---- InfluxDB forecast ----
    try:
        lines = []
        for i, s in enumerate(slots):
            ts = int(s["time"].timestamp()) * 1_000_000_000
            tags = f"minutes_ahead={i*15},strategy={s['label']}"
            fields = (
                f"consumption_w={s['cons_w']:.1f},"
                f"pv_w={s['pv_w']:.1f},"
                f"price_ct={s['price']*100:.3f},"
                f"setpoint_w={s['batt_w']}i,"
                f"grid_import_w={s['grid_w']:.1f},"
                f"soc_start_pct={s['soc_start_pct']:.1f},"
                f"soc_end_pct={s['soc_pct']:.1f}"
            )
            lines.append(f"energy_optimizer_forecast,{tags} {fields} {ts}")
        body = "\n".join(lines).encode("utf-8")
        params = f"db={CONFIG.influx.database}&u={CONFIG.influx.username}&p={CONFIG.influx.password}"
        url = f"{CONFIG.influx.write_url}?{params}"
        # In pyscript you would use aiohttp here:
        # async with aiohttp.ClientSession() as session:
        #     async with session.post(url, data=body,
        #                             headers={"Content-Type":"application/octet-stream"}):
        #         ...
        if CONFIG.general.log_debug:
            print(f"[INFLUX] forecast written ({len(lines)} points)")
    except Exception as e:
        if CONFIG.general.log_debug:
            print(f"[INFLUX] write error: {e}")
