# Energy Optimizer — Home Assistant pyscript

A strategic battery energy optimizer for Home Assistant, implemented as a
pyscript script. It solves a 24-slot Linear Program (or falls back to a
heuristic) every 30 minutes to minimize grid electricity costs by
intelligently charging and discharging a home battery based on real-time
EPEX spot prices, solar PV forecasts, and historical consumption patterns.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration Reference](#configuration-reference)
5. [How It Works](#how-it-works)
6. [Operating Modes](#operating-modes)
7. [24h Outlook Card](#24h-outlook-card)
8. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
EPEX Spot Prices          ┐
Solcast PV Forecast       ├──▶  Strategic Optimizer (this script)
InfluxDB Consumption      ┘          │
                                     ▼
                          input_number.energy_optimizer_mode_id
                          input_number.energy_optimizer_setpoint
                                     │
                                     ▼
                          Tactical Automation (separate HA automation)
                                     │
                                     ▼
                               Inverter / Battery
```

The script is a **planning layer only**. It writes a mode ID and a watt
setpoint to two `input_number` helpers every 30 minutes. A separate HA
automation reads those helpers and sends commands to the inverter in real
time. This separation means the inverter always follows the live grid
sensor (shrdzm), while the optimizer only updates the strategy periodically.

---

## Requirements

### Home Assistant integrations

| Integration | Purpose |
|---|---|
| [pyscript (HACS)](https://github.com/custom-components/pyscript) | Runs this script |
| [EPEX Spot (HACS)](https://github.com/mampfes/hacs_epex_spot) | Hourly day-ahead spot prices |
| [Solcast (HACS)](https://github.com/BJReplay/ha-solcast-solar) | PV generation forecast |
| InfluxDB (built-in) | Historical consumption data |
| shrdzm or equivalent | Real-time grid power sensor (used by tactical automation) |

### Python packages (auto-available in pyscript)

- `scipy` — LP solver (HiGHS backend via `scipy.optimize.linprog`)
- `aiohttp` — async HTTP for InfluxDB queries
- `pytz` — timezone handling

### File system

- `/config/www/` must exist (created automatically by HA in most installs)
- `/config/pyscript/energy_optimizer.py` — this script

---

## Installation

1. Install **pyscript** via HACS and enable it in `configuration.yaml`:

```yaml
pyscript:
  allow_all_imports: true
```

2. Add the required `input_number` and `input_text` helpers to
   `configuration.yaml`:

```yaml
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
```

3. Add the `command_line` sensor for the Lovelace outlook card:

```yaml
sensor:
  - platform: command_line
    name: energy_optimizer_outlook
    command: "cat /config/www/energy_outlook.md"
    scan_interval: 60
```

4. Copy `energy_optimizer.py` to `/config/pyscript/energy_optimizer.py`.

5. Restart Home Assistant.

---

## Configuration Reference

All configuration is at the top of `energy_optimizer.py`. No other files
need to be edited.

---

### Optimization mode

```python
USE_LP_OPTIMIZER = True
```

| Value | Behaviour |
|---|---|
| `True` | Uses the scipy HiGHS Linear Program — globally optimal across all 96 slots simultaneously. Recommended. |
| `False` | Uses the P25/P75 heuristic — simpler, faster, no scipy dependency. Useful for debugging or low-resource installs. |

---

### Hardware constants

```python
BATTERY_SIZE_WH = 2760
```
Total usable battery capacity in watt-hours. Used by the LP to track the
state of charge across the 24h planning horizon. Set this to your actual
battery nameplate capacity.

```python
OUTPUT_MIN_W = -1200
OUTPUT_MAX_W =  1200
```
Inverter output limits in watts. `OUTPUT_MIN_W` is negative (charging from
grid draws power into the battery). `OUTPUT_MAX_W` is the maximum discharge
rate. These are hard clamped in both the LP bounds and the heuristic — the
optimizer will never request a setpoint outside this range.

```python
BATTERY_FULL_PCT    = 98
BATTERY_TRICKLE_PCT = 96
BATTERY_EMPTY_PCT   = 15
```
SOC thresholds that control mode transitions:

- `BATTERY_FULL_PCT` — above this the script enters **TRICKLE** mode and
  stops charging. Slightly below 100% to prevent BMS stress.
- `BATTERY_TRICKLE_PCT` — hysteresis lower bound. While SOC is between
  `BATTERY_TRICKLE_PCT` and `BATTERY_FULL_PCT` the script applies a gentle
  trickle charge (`-BATTERY_TRICKLE_W`) to bring the battery back to full
  without overcharging.
- `BATTERY_EMPTY_PCT` — LP hard floor. The optimizer will never plan
  discharges that would bring the battery below this level. Also used as the
  "available energy" baseline in the heuristic.

```python
GRID_DEADZONE_W   = 10
BATTERY_TRICKLE_W = 10
```
- `GRID_DEADZONE_W` — setpoints within ±10W of zero are treated as
  **BALANCE** (grid consumption) mode. Prevents the inverter hunting around
  zero.
- `BATTERY_TRICKLE_W` — the gentle charge/discharge wattage applied during
  the trickle hysteresis band (see above).

---

### LP tuning

```python
DISCHARGE_PENALTY = 0.0001
```
A tiny cost (€/Wh equivalent) added to every watt of discharge in the LP
objective. This prevents the LP from over-discharging the battery beyond
the actual load when prices are equal across slots — without it, the solver
may choose to discharge the full `OUTPUT_MAX_W` even when only 200W is
needed. Keep this well below the cheapest expected spot price (~0.05
€/kWh). Increasing it makes the optimizer more conservative about
discharging; decreasing it allows more aggressive discharge.

---

### Behaviour flags

```python
ALLOW_EXPORT = False
```
Reserved for future use. When `True`, the LP will be allowed to plan
setpoints that export power to the grid (negative grid import). Currently
the LP bounds already enforce no-export via the `net_load` upper bound on
`x[t]`, so this flag is a placeholder for a future feed-in tariff mode.

---

### InfluxDB

```python
INFLUX_URL    = "http://localhost:8086/query"
INFLUX_DB     = "homeassistant"
INFLUX_USER   = "homeassistant"
INFLUX_PASS   = "hainflux!"
INFLUX_ENTITY = "total_consumption"
INFLUX_UNIT   = "W"
```

- `INFLUX_URL` — full URL to the InfluxDB HTTP query endpoint. Change the
  host/port if InfluxDB runs on a different machine or port.
- `INFLUX_DB` — InfluxDB database name. Default for the HA InfluxDB
  add-on is `homeassistant`.
- `INFLUX_USER` / `INFLUX_PASS` — InfluxDB credentials.
- `INFLUX_ENTITY` — the `entity_id` value stored in InfluxDB for the total
  household consumption sensor. Must match exactly what HA writes to
  InfluxDB (check with `SHOW TAG VALUES WITH KEY = "entity_id"`).
- `INFLUX_UNIT` — the InfluxDB measurement name. This is typically the
  unit of measurement (`W` or `kW`). If your sensor reports in `kW`, set
  this to `kW` — the script does **not** auto-convert; consumption values
  will be treated as watts regardless.

The script queries the **4 most recent same-weekday** days (e.g. the last
4 Saturdays) and averages the 15-minute consumption profiles to build a
typical-day forecast. If InfluxDB is unavailable or returns no data, a
hardcoded hourly fallback profile is used automatically.

---

### HA entity IDs

```python
E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER  = "sensor.ezhi_battery_power"
E_PRICE_DATA     = "sensor.epex_spot_data_total_price"
E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"
```

- `E_BATTERY_SOC` — battery state of charge sensor (0–100%). Used at every
  cycle to determine current SOC and apply trickle overrides.
- `E_BATTERY_POWER` — battery power sensor (W). Currently read but reserved
  for future real-time correction of LP initial state.
- `E_PRICE_DATA` — EPEX Spot sensor entity. Must expose a `data` attribute
  containing a list of `{start_time, price_per_kwh}` dicts — this is the
  default format from the EPEX Spot HACS integration.
- `E_SOLAR_HOUR` — Solcast next-hour forecast sensor. The script reads the
  `forecast` (or `detailedForecast` / `forecasts`) attribute for per-slot
  PV estimates in kW, which are converted to W internally.
- `E_SOLAR_TOMORROW` — reserved for future multi-day planning horizon
  extension.

---

### Output helpers

```python
E_MODE_ID  = "input_number.energy_optimizer_mode_id"
E_SETPOINT = "input_number.energy_optimizer_setpoint"
```

These are written every 30-minute cycle and read by the tactical HA
automation:

| Mode ID | Mode name | Meaning |
|---|---|---|
| 0 | BALANCE | Follow grid in real time; setpoint ≈ 0W |
| 1 | GRID_CHARGE | Charge battery from grid at `OUTPUT_MIN_W` |
| 2 | DISCHARGE | Discharge battery to cover load |
| 3 | TRICKLE | Gentle charge/discharge near full SOC |

```python
E_STATUS_MODE   = "input_text.energy_optimizer_mode"
E_STATUS_REASON = "input_text.energy_optimizer_reason"
```

Human-readable dashboard helpers. `E_STATUS_MODE` shows the current mode
icon and label (e.g. `🏭 GRID CONSUMPTION`). `E_STATUS_REASON` contains a
full natural-language explanation of why the current mode was chosen,
including price context, SOC, and LP guidance.

---

### Outlook file

```python
OUTLOOK_FILE = "/config/www/energy_outlook.md"
```

Path where the 24h outlook Markdown table is written after each hourly
cycle. Served via HA's `/local/` static file server and read by the
`command_line` sensor for display in the Lovelace Markdown card. The
directory `/config/www/` must exist.

---

### Timezone

```python
TZ = pytz.timezone("Europe/Vienna")
```

All datetime calculations use this timezone. Change to your local timezone
(e.g. `Europe/Berlin`, `Europe/Amsterdam`) to ensure slot alignment with
EPEX prices and solar forecasts is correct.

---

## How It Works

### 1. Data collection (every 30 min)

- **SOC** is read from `E_BATTERY_SOC`.
- **Consumption forecast** is built from InfluxDB (4 same-weekday days,
  15-min averages), falling back to a hardcoded hourly profile.
- **Solar forecast** is read from the Solcast sensor attributes and
  converted to per-15min watt values.
- **EPEX prices** are read from the EPEX Spot sensor `data` attribute.
  These are hourly day-ahead prices in €/kWh, applied to all 4 x 15-min
  slots within each hour.

### 2. Schedule building

96 forward-looking 15-min slots are assembled starting from the current
time. Each slot contains:

| Field | Meaning |
|---|---|
| `cons` | Expected household consumption (W) |
| `solar` | Expected PV generation (W) |
| `price` | EPEX spot price (€/kWh) |
| `net` | `cons - solar` — positive = deficit, negative = surplus |

### 3. Optimization

**LP mode** (`USE_LP_OPTIMIZER = True`):

The LP minimizes total grid cost over all 96 slots simultaneously:

```
minimize  Σ  price[t] · DT · s[t]  +  DISCHARGE_PENALTY · DT · x[t]

subject to:
  s[t] ≥ net[t] - x[t]          (grid slack covers unmet load)
  OUTPUT_MIN_W ≤ x[t] ≤ min(OUTPUT_MAX_W, net[t])
  s[t] ≥ 0
  E_min ≤ E_now - Σ x[τ]·DT ≤ E_max   for all t
```

Where `x[t]` is the inverter setpoint (W) and `s[t]` is grid import (W).

**Heuristic mode** (`USE_LP_OPTIMIZER = False`):

Applies simple P25/P75 rules per slot:
- Price ≤ P25 → charge from grid at `OUTPUT_MIN_W`
- Price ≥ P75 → discharge to cover load
- Mid price → hold or discharge depending on future high-price demand and
  expected PV recharge availability

### 4. Trickle override

Before writing outputs, the SOC-based trickle logic overrides the optimizer
result for slot-0:

- SOC ≥ `BATTERY_FULL_PCT` and PV surplus → spill surplus to grid
- SOC ≥ `BATTERY_FULL_PCT` no surplus → TRICKLE (+10W)
- SOC between `BATTERY_TRICKLE_PCT` and `BATTERY_FULL_PCT` → TRICKLE (-10W)
- Otherwise → use optimizer result unchanged

### 5. Critical SOC guard

A `state_trigger` on `E_BATTERY_SOC` runs independently of the 30-min
cycle. If SOC drops below **12%**, the inverter is immediately forced to
0W (BALANCE mode) and a persistent HA notification is created. The
optimizer resumes normally at the next 30-min cycle.

---

## Operating Modes

| Mode | Icon | Mode ID | Setpoint | Condition |
|---|---|---|---|---|
| Grid consumption | 🏭 | 0 | ~0W | Mid price, no strong signal |
| Grid charge | ⚡ | 1 | `OUTPUT_MIN_W` | Price ≤ P25 |
| Discharge | 🔋 | 2 | 0–`OUTPUT_MAX_W` | Price ≥ P75 |
| Trickle | 🌿 | 3 | ±10W | SOC near `BATTERY_FULL_PCT` |

### 24h outlook slot labels

| Label | Symbol | Meaning |
|---|---|---|
| Grid consumption | 🏭 | Buy from grid; battery idle |
| Charge from grid | ⚡ | Actively charging battery |
| Cover load | ⚖️ | Discharging battery to cover load at mid price |
| Discharge (peak price) | 🔋 | Discharging at high price slot |
| PV surplus / spill | ☀️ | PV generation exceeds load |

---

## 24h Outlook Card

The script writes a Markdown table to `/config/www/energy_outlook.md` once
per hour (on the first 30-min cycle of each hour). A `command_line` sensor
reads this file and a Lovelace Markdown card renders it.

### configuration.yaml

```yaml
sensor:
  - platform: command_line
    name: energy_optimizer_outlook
    command: "cat /config/www/energy_outlook.md"
    scan_interval: 60
```

### Lovelace card

```yaml
type: markdown
title: ⚡ Energy Optimizer — 24h Outlook
content: "{{ states('sensor.energy_optimizer_outlook') }}"
```

### Example output

```
LP optimizer | SOC 72% | P25 0.0891 · P75 0.1134 €/kWh  (updated 28.02.2026 10:00)

| Time          | Strategy                   | Price               |
|---------------|----------------------------|---------------------|
| 10:00–11:00   | 🏭 Grid consumption        | 0.1050 €/kWh        |
| 11:00–14:00   | ☀️ PV surplus / spill      | 0.0923 €/kWh        |
| 14:00–17:00   | ⚖️ Cover load              | 0.1080 €/kWh        |
| 17:00–19:00   | 🔋 Discharge (peak price)  | 0.1340 €/kWh        |
| 19:00–22:00   | 🏭 Grid consumption        | 0.0914 €/kWh        |
| 22:00–06:00   | ⚡ Charge from grid         | 0.0710 €/kWh        |
```

---

## Troubleshooting

### `scipy/numpy import failed: partially initialized module`
Move `from scipy.optimize import linprog` to the top-level imports (outside
any function). pyscript's sandbox can trigger circular imports when scipy is
imported lazily inside a function call on first load after an HA restart.

### `Could not write outlook file: name 'open' is not defined`
pyscript sandboxes built-in functions. Use `builtins.open` instead of `open`
and import `builtins` at the top of the script.

### `No EPEX price data available`
The EPEX Spot integration publishes new day-ahead prices around 13:00–14:00
CET. Before that time only today's prices are available. The sensor may also
show `unavailable` briefly after HA restart — the optimizer will skip the
cycle and retry at the next 30-min trigger.

### `No InfluxDB data — using fallback consumption profile`
Check that:
- `INFLUX_ENTITY` matches the exact `entity_id` tag stored in InfluxDB
- `INFLUX_UNIT` matches the InfluxDB measurement name (`W` or `kW`)
- The InfluxDB add-on is running and accessible at `INFLUX_URL`
- The consumption sensor has been recording for at least 1 week (the script
  queries 4 same-weekday days back)

### LP solver returns status != 0
The HiGHS solver may return infeasible if the battery constraints are
contradictory (e.g. `BATTERY_EMPTY_PCT` ≥ `BATTERY_FULL_PCT`, or initial
SOC outside bounds). Check that:
- `BATTERY_EMPTY_PCT` < `BATTERY_TRICKLE_PCT` < `BATTERY_FULL_PCT`
- Current SOC reported by `E_BATTERY_SOC` is a valid number between 0–100
- `BATTERY_SIZE_WH` is correctly set to your actual battery capacity

### Outlook card shows `unknown`
The `command_line` sensor will show `unknown` until the first hourly outlook
cycle runs. Force a run via:
**Developer Tools → Actions → `pyscript.energy_optimizer_force_run`**
then wait up to `scan_interval` seconds (default 60s) for the sensor to
update.
