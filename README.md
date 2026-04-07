# Energy Optimizer for Home Assistant (pyscript)

A strategic energy‑management layer that runs every 15 minutes (and on EPEX price updates) to decide how the home battery should be charged or discharged.  
It combines historical consumption, actual/solar forecast, and spot‑market prices, solves an optimization problem (linear programming by default, with a heuristic fallback), applies state‑of‑charge (SOC) based overrides, and writes the resulting mode and power setpoint to Home Assistant helpers for the tactical automation layer.

---

## Table of Contents
- [Overview](#overview)
- [File Structure](#file-structure)
- [Configuration (`energy_optimizer_config.yaml`)](#configuration-energy_optimizer_configyaml)
- [Module Details](#module-details)
  - [_config.py](#_configpy)
  - [data_fetcher.py](#data_fetcherpy)
  - [optimizer.py](#optimizerpy)
  - [output_handler.py](#output_handlerpy)
  - [main.py (entry point)](#mainpy-entry-point)
- [Outputs & Example](#outputs--example)
- [How to Deploy / Update](#how-to-deploy--update)
- [Testing & Extending](#testing--extending)
- [License](#license)

---  

## Overview

The optimizer works in four logical steps each cycle:

1. **Data acquisition** – pull historical consumption and actual solar production from InfluxDB, blend actuals with Solcast forecast, and read the latest EPEX spot prices (plus a fixed network fee).  
2. **Schedule building** – create a 24‑hour horizon of 96 × 15‑minute slots, each holding load, solar generation, price, and net power (load − solar).  
3. **Optimization** –  
   *If `use_lp_optimizer: true`* – a linear‑programming model (via `scipy.optimize.linprog`) minimizes cost over the horizon while respecting battery limits, charge/discharge efficiencies, and an optional export‑block.  
   *If `use_lp_optimizer: false`* – a rule‑based heuristic computes a feasible setpoint vector.  
4. **SOC overrides & output** – the raw optimizer setpoint for the current slot is refined with real‑time SOC guards (trickle band, anti‑curtail, grid‑charge suppression). The final mode (`GRID_CHARGE`, `DISCHARGE`, `BALANCE`, `TRICKLE`) and setpoint (in W) are written to `input_number` helpers, status texts are updated, and a 24‑hour outlook (Markdown + CSV) plus a forecast series are persisted to InfluxDB.

The result is a **setpoint** that the Home Assistant tactical layer (e.g., an automation that controls the inverter) can act upon immediately, while the outlook gives the user a visual preview of the planned strategy.

---  

## File Structure

energy_optimizer/
├─ energy_optimizer_config.yaml # ← all tunable parameters
├─ _config.py # YAML loader (singleton CONFIG)
├─ data_fetcher.py # InfluxDB / Solcast / EPEX calls
├─ optimizer.py # schedule builder, LP & heuristic, SOC overrides
├─ output_handler.py # HA state updates, file/InfluxDB logging
└─ main.py # pyscript entry point (triggers, service, SOC‑critical)


All files reside in the same folder (e.g., `/config/pyscript/`). The folder is automatically on the Python path for pyscript.

---  

## Configuration (`energy_optimizer_config.yaml')

Every value likely to change between installations lives in this YAML file. Edit it and reload the pyscript – no code changes needed.

```yaml
# energy_optimizer_config.yaml
general:
  timezone: Europe/Vienna          # IANA timezone name
  use_lp_optimizer: true           # false → heuristic fallback
  log_debug: true                  # verbose logging to HA log

influx:
  url: http://localhost:8086/query
  write_url: http://localhost:8086/write
  database: homeassistant
  username: homeassistant
  password: hainflux!
  unit: W                          # unit stored in InfluxDB
  entity_consumption: total_consumption
  entity_pv: ezhi_photovoltaic_power
  entity_soc: sensor.ezhi_battery_state_of_charge
  price_sensor: sensor.epex_spot_data_total_price

solcast:
  hour: sensor.solcast_pv_forecast_forecast_next_hour
  today: sensor.solcast_pv_forecast_forecast_today
  tomorrow: sensor.solcast_pv_forecast_forecast_tomorrow

files:
  outlook_md: /config/www/energy_outlook.md
  forecast_csv: /config/www/energy_forecast.csv

battery:
  size_wh: 2760                     # usable capacity in Wh
  charge_efficiency: 0.95           # fraction of input energy stored
  discharge_efficiency: 0.95        # fraction of stored energy delivered
  output_min_w: -1200               # max charge (negative)
  output_max_w: 1200                # max discharge (positive)
  full_pct: 98                      # SOC considered “full”
  trickle_pct: 96                   # start of trickle‑band
  empty_pct: 15                     # SOC considered “empty”
  grid_deadzone_w: 10               # |setpoint| below this is treated as 0 W
  trickle_w: 10                     # small charge power used in trickle‑band

grid:
  network_fee_ct_per_kwh: 10.5      # additional cost per kWh (ct)
  allow_export: false               # set true if you want to permit export to grid

solar:
  pv_nameplate_wp: 1200             # name‑plate peak power of the PV array (Wp)
  pv_threshold_w: 120               # treated as “producing” when ≥ this value (W)

optimization:
  schedule_slots: 96                # number of 15‑min slots in the horizon (96 = 24 h)
  discharge_penalty: 0.0001         # small cost added to discharging in LP objective
  opportunity_cost_weight: 0.5      # 0‑1 weight for look‑ahead term in LP
  grid_charge_soc_block_pct: 70     # above this SOC grid‑charging is blocked
  grid_charge_soc_cheap_pct: 50     # between cheap‑pct and block‑pct only allow at p25 price or cheaper

pricing:
  # Fallback hourly prices (ct/kWh) used if the EPEX sensor fails
  fallback_hourly_ct:
    0: 8.0,  1: 7.5,  2: 7.0,  3: 6.5,  4: 6.5,  5: 7.0
    6: 18.0, 7: 22.0,  8: 20.0,  9: 15.0, 10: 11.0, 11: 8.0
    12: 6.0, 13: 6.0, 14: 7.0, 15: 9.0, 16: 14.0, 17: 22.0
    18: 26.0,19: 28.0, 20: 24.0, 21: 18.0, 22: 13.0, 23: 10.0
```

*Notes*  
- All times are interpreted in the `general.timezone`.  
- The `fallback_hourly_ct` map is only used when the EPEX sensor (`price_sensor`) returns no data.  
- Changing any value (e.g., battery size, efficiencies, fees, file paths) takes effect after a pyscript reload.

---  

## Module Details

### `_config.py`
```python
# _config.py
"""
Central configuration loader – reads energy_optimizer_config.yaml once
and makes the values available as attributes (CONFIG.<section>.<key>).
"""
import os, yaml
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "energy_optimizer_config.yaml")
with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    _raw = yaml.safe_load(f)

class _Conf:
    def __init__(self, data):
        for k, v in data.items():
            setattr(self, k, _Conf(v) if isinstance(v, dict) else v)

CONFIG = _Conf(_raw)
```
*Purpose*: Provides a single, importable `CONFIG` object so every module can read `CONFIG.battery.size_wh`, `CONFIG.influx.url`, etc., without repeatedly opening the YAML.

---  

### `data_fetcher.py`

| Function | Description |
|----------|-------------|
| `_influx_query(q)` | Internal helper: performs a GET request to the InfluxDB HTTP API and returns the JSON payload. |
| `fetch_historical_consumption()` | Returns a dictionary `{(hour, quarter): mean_power_W}` for the last four weeks (or a simple fallback if no data). |
| `fetch_solar_actuals()` | Returns `{hour: actual_W}` for hours already elapsed today (based on InfluxDB mean per hour). |
| `blend_solar_forecast(actuals, state_getter)` | Combines actual solar production with Solcast forecast: past hours = 100 % actual, current hour = 50 % actual + 50 % scaled forecast, next `SOLAR_BLEND_HOURS` (fixed 2 h) blend, beyond that pure forecast. `state_getter` is a callable that mimics `state.getattr` for the Solcast entities (provided by the pyscript runtime). |
| `fetch_spot_prices()` | Returns `{(hour, quarter): price_€/kWh}` (EPEX price plus the network fee from config). On failure, uses the fallback hourly prices defined in the YAML. |

All functions are **async** where they perform network/DB I/O, allowing the pyscript event loop to remain responsive.

---  

### `optimizer.py`

| Function / Helper | Description |
|-------------------|-------------|
| `build_schedule(consumption, solar, prices)` | Constructs the list of 96 slot dictionaries (`i`, `time`, `cons`, `solar`, `price`, `net`). |
| `_solve_optimal_schedule(soc, schedule)` | Implements the linear‑programming model (objective: minimize cost + opportunity cost + small discharge penalty). Variables: discharge `b[t]` (≥0) and charge `g[t]` (≥0) for each slot. Constraints enforce power balance, SOC limits, and optional export block. Returns a list of raw setpoint values (W) for each slot. |
| `_heuristic_schedule(soc, schedule)` | Fallback rule‑based method: classifies each slot into price tiers (P25, P75) and applies simple charging/discharging heuristics based on SOC, future high‑price windows, and expected PV recharge. |
| `get_optimal_setpoints(soc, schedule, use_lp=True)` | Public wrapper that selects LP or heuristic based on the flag. |
| `apply_trickle_override(soc, raw_sp, net, price, p25, p75)` | Takes the raw optimizer setpoint for the current slot and applies SOC‑based overrides: anti‑curtail discharge when battery full, trickle band, grid‑charge suppression at high SOC, and honoring discharge when price is expensive. Returns `(mode, final_setpoint)` where `mode` ∈ {"GRID_CHARGE","DISCHARGE","BALANCE","TRICKLE"}. |

All constants (battery size, efficiencies, thresholds, fees, etc.) are read from `CONFIG`.

---  

### `output_handler.py`

| Function | Description |
|----------|-------------|
| `write_ha_outputs(mode, setpoint)` | Writes the numeric mode ID (0‑3) and setpoint (W) to the `input_number` helpers. In pyscript replace the print statements with `input_number.set_value(...)`. |
| `update_status(mode, reason)` | Updates two `input_text` entities with a human‑readable mode label and a detailed reason string. |
| `log_24h_outlook(schedule, optimal_schedule, soc, use_lp)` | Builds per‑slot information (label, power flows, SOC evolution), aggregates consecutive equal‑label slots into windows, and writes: <br>• Markdown outlook to `CONFIG.files.outlook_md` <br>• CSV forecast to `CONFIG.files.forecast_csv` <br>• Forecast series to InfluxDB (measurement `energy_optimizer_forecast`). |
| (Internal) helper functions for window aggregation, markdown/table generation, CSV writing, InfluxDB line protocol formatting. |

If `CONFIG.general.log_debug` is `true`, the module prints concise debug messages to the HA log (useful during development).

---  

### `main.py` (Entry point)

This file wires the three layers together and provides the pyscript triggers:

| Trigger / Service | What it does |
|-------------------|--------------|
| `@time_trigger("cron(0,15,30,45 * * * *)") async def strategic_optimize()` | Main 15‑minute loop: fetches data, builds schedule, runs optimizer, applies overrides, writes HA outputs, updates status, and logs the outlook. |
| `@state_trigger(CONFIG.influx.price_sensor) async def on_price_update(**kwargs)` | Reacts to EPEX price updates by calling `strategic_optimize()` immediately (so the optimizer reacts to price changes without waiting for the next 15‑min tick). |
| `@state_trigger(CONFIG.influx.entity_soc) def on_soc_critical(**kwargs)` | Emergency handling: if SOC drops below 12 % (hard‑coded; could be moved to YAML), forces mode = BALANCE and setpoint = 0 W, creates a persistent notification, and logs a warning. |
| `@service async def energy_optimizer_force_run()` | Allows manual triggering via *Developer Tools → Services → pyscript.energy_optimizer_force_run* (useful for testing). |

All heavy lifting (data fetching, optimization, output) resides in the imported modules; this file remains thin and easy to follow.

---  

## Outputs & Example

### Home Assistant Entities Updated

| Entity ID | Type | Meaning |
|-----------|------|---------|
| `input_number.energy_optimizer_mode_id` | number | 0 = BALANCE, 1 = GRID_CHARGE, 2 = DISCHARGE, 3 = TRICKLE |
| `input_number.energy_optimizer_setpoint` | number | Power setpoint in watts (negative = charging, positive = discharging) |
| `input_text.energy_optimizer_mode` | text | Human‑readable mode with an emoji (e.g., “⚡ GRID CHARGE”) |
| `input_text.energy_optimizer_reason` | text | Short explanation why the mode/setpoint was chosen |

### Markdown Outlook (`/config/www/energy_outlook.md`)

```markdown
**LP optimizer** | SOC **72%** | P25 8.5 · P75 22.0 ct/kWh _(updated 07.04.2026 16:30)_

| Time | Strategy | Price | Consumption | PV forecast | Grid import | Batt setpoint | SOC end |
|------|----------|-------|-------------|-------------|-------------|---------------|---------|
| `00:00–00:15` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 70% |
| `00:15–00:30` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 68% |
| `00:30–00:45` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 66% |
| `00:45–01:00` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 64% |
| `01:00–01:15` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 62% |
| `01:15–01:30` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 60% |
| `01:30–01:45` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 58% |
| `01:45–02:00` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 56% |
| `02:00–02:15` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 54% |
| `02:15–02:30` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 52% |
| `02:30–02:45` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 50% |
| `02:45–03:00` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 48% |
| `03:00–03:15` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 46% |
| `03:15–03:30` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 44% |
| `03:30–03:45` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 42% |
| `03:45–04:00` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 40% |
| `04:00–04:15` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 38% |
| `04:15–04:30` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 36% |
| `04:30–04:45` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 34% |
| `04:45–05:00` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 34% |
| `05:00–05:15` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 34% |
| `05:15–05:30` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 34% |
| `05:30–05:45` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 34% |
| `05:45–06:00` (15min) | ⚡ Charge from grid | 8.5 ct | 300 W |   0 W | +300 W | –1200 W | 34% |
| `06:00–06:15` (15min) | ☀️ Charge from PV | 7.5 ct | 250 W | 100 W | +150 W | –1200 W | 34% |
| `06:15–06:30` (15min) | ☀️ Charge from PV | 7.5 ct | 200 W | 150 W |  +50 W | –1200 W | 34% |
| `06:30–06:45` (15min) | ☀️ Charge from PV | 7.5 ct | 150 W | 200 W |  –50 W | –1000 W | 36% |
| `06:45–07:00` (15min) | ☀️ Charge from PV | 7.5 ct | 100 W | 250 W | –150 W |  –800 W | 38% |
| `07:00–07:15` (15min) | ☀️ Charge from PV | 7.5 ct |  50 W | 300 W | –250 W |  –600 W | 40% |
| `07:15–07:30` (15min) | ☀️ Charge from PV | 7.5 ct |   0 W | 350 W | –350 W |  –400 W | 42% |
| `07:30–07:45` (15min) | ☀️ Charge from PV | 7.5 ct |   0 W | 400 W | –400 W |  –200 W | 44% |
| `07:45–08:00` (15min) | ☀️ Charge from PV | 7.5 ct |   0 W | 450 W | –450 W |    0 W | 46% |
| `08:00–08:15` (15min) | ⚖️ Cover load | 15.0 ct | 500 W | 500 W |   0 W |    0 W | 46% |
| `08:15–08:30` (15min) | ⚖️ Cover load | 15.0 ct | 550 W | 500 W | + 50 W |    0 W | 46% |
| `08:30–08:45` (15min) | ⚖️ Cover load | 15.0 ct | 600 W | 500 W | +100 W |    0 W | 46% |
| `08:45–09:00` (15min) | ⚖️ Cover load | 15.0 ct | 650 W | 500 W | +150 W |    0 W | 46% |
| `09:00–09:15` (15min) | ⚖️ Cover load | 15.0 ct | 700 W | 500 W | +200 W |    0 W | 46% |
| `09:15–09:30` (15min) | ⚖️ Cover load | 15.0 ct | 750 W | 500 W | +250 W |    0 W | 46% |
| `09:30–09:45` (15min) | ⚖️ Cover load | 15.0 ct | 800 W | 500 W | +300 W |    0 W | 46% |
| `09:45–10:00` (15min) | ⚖️ Cover load | 15.0 ct | 850 W | 500 W | +350 W |    0 W | 46% |
| `10:00–10:15` (15min) | ⚖️ Cover load | 15.0 ct | 900 W | 500 W | +400 W |    0 W | 46% |
| `10:15–10:30` (15min) | ⚖️ Cover load | 15.0 ct | 950 W | 500 W | +450 W |    0 W | 46% |
| `10:30–10:45` (15min) | ⚖️ Cover load | 15.0 ct |1000 W | 500 W | +500 W |    0 W | 46% |
| `10:45–11:00` (15min) | ⚖️ Cover load | 15.0 ct |1050 W | 500 W | +550 W |    0 W | 46% |
| `11:00–11:15` (15min) | ⚖️ Cover load | 15.0 ct |1100 W | 500 W | +600 W |    0 W | 46% |
| `11:15–11:30` (15min) | ⚖️ Cover load | 15.0 ct |1150 W | 500 W | +650 W |    0 W | 46% |
| `11:30–11:45` (15min) | ⚖️ Cover load | 15.0 ct |1200 W | 500 W | +700 W |    0 W | 46% |
| `11:45–12:00` (15min) | ⚖️ Cover load | 15.0 ct |1250 W | 500 W | +750 W |    0 W | 46% |
| `12:00–12:15` (15min) | ⚖️ Cover load | 15.0 ct |1300 W | 500 W | +800 W |    0 W | 46% |
| `12:15–12:30` (15min) | ⚖️ Cover load | 15.0 ct |1350 W | 500 W | +850 W |    0 W | 46% |
| `12:30–12:45` (15min) | ⚖️ Cover load | 15.0 ct |1400 W | 500 W | +900 W |    0 W | 46% |
| `12:45–13:00` (15min) | ⚖️ Cover load | 15.0 ct |1400 W | 500 W | +900 W |    0 W | 46% |
| `13:00–13:15` (15min) | ⚖️ Cover load | 15.0 ct |1350 W | 500 W | +850 W |    0 W | 46% |
| `13:15–13:30` (15min) | ⚖️ Cover load | 15.0 ct |1300 W | 500 W | +800 W |    0 W | 46% |
| `13:30–13:45` (15min) | ⚖️ Cover load | 15.0 ct |1250 W | 500 W | +750 W |    0 W | 46% |
| `13:45–14:00` (15min) | ⚖️ Cover load | 15.0 ct |1200 W | 500 W | +700 W |    0 W | 46% |
| `14:00–14:15` (15min) | ⚖️ Cover load | 15.0 ct |1150 W | 500 W | +650 W |    0 W | 46% |
| `14:15–14:30` (15min) | ⚖️ Cover load | 15.0 ct |1100 W | 500 W | +600 W |    0 W | 46% |
| `14:30–14:45` (15min) | ⚖️ Cover load | 15.0 ct |1050 W | 500 W | +550 W |    0 W | 46% |
| `14:45–15:00` (15min) | ⚖️ Cover load | 15.0 ct |1000 W | 500 W | +500 W |    0 W | 46% |
| `15:00–15:15` (15min) | ⚖️ Cover load | 15.0 ct | 950 W | 500 W | +450 W |    0 W | 46% |
| `15:15–15:30` (15min) | ⚖️ Cover load | 15.0 ct | 900 W | 500 W | +400 W |    0 W | 46% |
| `15:30–15:45` (15min) | ⚖️ Cover load | 15.0 ct | 850 W | 500 W | +350 W |    0 W | 46% |
| `15:45–16:00` (15min) | ⚖️ Cover load | 15.0 ct | 800 W | 500 W | +300 W |    0 W | 46% |
| `16:00–16:15` (15min) | ⚖️ Cover load | 15.0 ct | 750 W | 500 W | +250 W |    0 W | 46% |
| `16:15–16:30` (15min) | ⚖️ Cover load | 15.0 ct | 700 W | 500 W | +200 W |    0 W | 46% |
| `16:30–16:45` (15min) | ⚖️ Cover load | 15.0 ct | 650 W | 500 W | +150 W |    0 W | 46% |
| `16:45–17:00` (15min) | ⚖️ Cover load | 15.0 ct | 600 W | 500 W | +100 W |    0 W | 46% |
| `17:00–17:15` (15min) | ⚖️ Cover load | 15.0 ct | 550 W | 500 W | + 50 W |    0 W | 46% |
| `17:15–17:30` (15min) | ⚖️ Cover load | 15.0 ct | 500 W | 500 W |   0 W |    0 W | 46% |
| `17:30–17:45` (15min) | ⚖️ Cover load | 15.0 ct | 450 W | 500 W | –50 W | –200 W | 48% |
| `17:45–18:00` (15min) | ⚖️ Cover load | 15.0 ct | 400 W | 500 W | –100 W | –400 W | 50% |
| `18:00–18:15` (15min) | ⚖️ Cover load | 15.0 ct | 350 W | 500 W | –150 W | –600 W | 52% |
| `18:15–18:30` (15min) | ⚖️ Cover load | 15.0 ct | 300 W | 500 W | –200 W | –800 W | 54% |
| `18:30–18:45` (15min) | ⚖️ Cover load | 15.0 ct | 250 W | 500 W | –250 W | –1000 W | 56% |
| `18:45–19:00` (15min) | ⚖️ Cover load | 15.0 ct | 200 W | 500 W | –300 W | –1200 W | 58% |
| `19:00–19:15` (15min) | ⚖️ Cover load | 15.0 ct | 150 W | 500 W | –350 W | –1200 W | 60% |
| `19:15–19:30` (15min) | ⚖️ Cover load | 15.0 ct | 100 W | 500 W | –400 W | –1200 W | 62% |
| `19:30–19:45` (15min) | ⚖️ Cover load | 15.0 ct |  50 W | 500 W | –450 W | –1200 W | 64% |
| `19:45–20:00` (15min) | ⚖️ Cover load | 15.0 ct |   0 W | 500 W | –500 W | –1200 W | 66% |
| `20:00–20:15` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 62% |
| `20:15–20:30` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 58% |
| `20:30–20:45` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 54% |
| `20:45–21:00` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 50% |
| `21:00–21:15` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 46% |
| `21:15–21:30` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 42% |
| `21:30–21:45` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 38% |
| `21:45–22:00` (15min) | ⚡ Discharge (peak) | 22.0 ct | 300 W |   0 W | –300 W | +800 W | 34% |
| `22:00–22:15` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct | 200 W |   0 W | –200 W | +600 W | 32% |
| `22:15–22:30` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct | 200 W |   0 W | –200 W | +600 W | 30% |
| `22:30–22:45` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct | 200 W |   0 W | –200 W | +600 W | 28% |
| `22:45–23:00` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct | 200 W |   0 W | –200 W | +600 W | 26% |
| `23:00–23:15` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct | 150 W |   0 W | –150 W | +400 W | 24% |
| `23:15–23:30` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct | 100 W |   0 W | –100 W | +200 W | 22% |
| `23:30–23:45` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct |  50 W |   0 W |  –50 W | +100 W | 20% |
| `23:45–00:00` (15min) | ⚡ Discharge (off‑peak) | 18.0 ct |   0 W |   0 W |   0 W |   0 W | 20% |
```

*Note*: The above table is a simplified illustrative example; actual output will reflect the real data fetched each cycle.

---  

## How to Deploy / Update

1. **Copy the files** into your pyscript folder, e.g., `/config/pyscript/`.

/config/pyscript/
├─ energy_optimizer_config.yaml
├─ _config.py
├─ data_fetcher.py
├─ optimizer.py
├─ output_handler.py
└─ main.py

2. **Adjust the YAML** if needed (battery size, efficiencies, timezone, file paths, etc.).  
3. **Reload the pyscript** in Home Assistant: *Developer Tools → YAML → `pyscript: reload`* or restart Home Assistant.  
4. Verify that the entities `input_number.energy_optimizer_mode_id`, `input_number.energy_optimizer_setpoint`, `input_text.energy_optimizer_mode`, and `input_text.energy_optimizer_reason` update as expected.  
5. Check the outlook file at `/config/www/energy_outlook.md` and the CSV at `/config/www/energy_forecast.csv`.  

---  

## Testing & Extending

- **Unit tests**: The `optimizer.py` module can be tested with pure Python dictionaries (no HA needed). Example test for `build_schedule` and `_solve_optimal_schedule`.  
- **Adding new data sources**: Extend `data_fetcher.py` with a new async function and import it in `main.py`.  
- **Changing the optimization objective**: Edit the objective construction in `_solve_optimal_schedule` (e.g., add a term for battery wear).  
- **Alternative heuristics**: Replace `_heuristic_schedule` or add a new strategy flag in the YAML.  

---  

## License

This project is provided as‑is under the MIT License. Feel free to modify and redistribute.  

---  

*End of README.*

