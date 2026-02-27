# Energy Optimizer for Home Assistant

A pyscript-based energy optimizer for Home Assistant that minimizes electricity
costs by controlling a battery inverter based on historical consumption patterns,
solar forecasts, and real-time EPEX spot prices.

---

## Requirements

### Hardware
- **Inverter:** APsystems EzHi (with local API enabled)
- **Battery:** Any battery with SOC and power sensors in HA
- **Smart meter:** Grid power sensor (positive = import, negative = export)
- **Solar forecast:** Solcast integration
- **Electricity pricing:** EPEX Spot integration (`ha_epex_spot`)
- **Historical data:** InfluxDB with Home Assistant recorder

### Home Assistant Integrations
- [pyscript](https://github.com/custom-components/pyscript) (via HACS)
- [APsystems EzHi](https://www.home-assistant.io/integrations/apsystems/)
- [Solcast PV Solar](https://github.com/BJReplay/ha-solcast-solar) (via HACS)
- [EPEX Spot](https://github.com/mampfes/ha_epex_spot) (via HACS)

---

## Installation

### 1. Install pyscript via HACS

1. Open HACS → Integrations
2. Search for **pyscript** and click Download
3. Restart Home Assistant when prompted
4. Go to **Settings → Integrations → Add Integration** and add pyscript

### 2. Enable pyscript imports

Add the following to your `/config/configuration.yaml`:

```yaml
pyscript:
  allow_all_imports: true
```

### 3. Add dashboard input_text helpers

Add the following to your `/config/configuration.yaml`:

```yaml
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

### 4. Copy the script

Only one file is needed. Create the folder `/config/pyscript/` if it does not
exist, then copy `energy_optimizer.py` into it:

```
/config/pyscript/energy_optimizer.py
```

No helper modules or additional files are required.

### 5. Restart Home Assistant

A **full restart** is required (not just a pyscript reload) for the first
installation, and also after adding `input_text` helpers to `configuration.yaml`.
After that, edits to the script only require a pyscript reload:

**Settings → Integrations → pyscript → three dots → Reload**

### 6. Verify it is running

Go to **Settings → System → Logs** and filter for `energy_optimizer`.
You should see lines like:

```
── Strategic optimization cycle (LP) ──
EPEX data slots found: 96
LP solved ✓ | Slot-0=+0W | Expected 24h grid cost=0.3241 €
Mode=BALANCE | SOC=72% | Price=0.1423 €/kWh | Optimizer=+0W → Applied=+0W
Status: ⚖️ BALANCE | LP optimizer: no strong charge/discharge signal at 0.1423 €/kWh ...
```

---

## How the Optimization Works

The optimizer runs two control loops simultaneously.

### Strategic Layer — every 15 minutes

Runs a full planning cycle over a 24-hour horizon (96 × 15-min slots) using
three data sources:

**1. Historical consumption (InfluxDB)**
Queries the past 4 occurrences of the same weekday and computes a 15-minute
mean load profile. This gives a realistic forecast of when the apartment will
consume power.

**2. Solar forecast (Solcast)**
Reads hourly PV production estimates. Combined with consumption, this yields
a `net_load` per slot:

```
net_load = consumption − solar
net_load > 0 → apartment needs power from battery or grid
net_load < 0 → solar surplus available to charge battery
```

**3. EPEX Spot prices**
Reads 15-minute electricity prices. Available for the current day; after
approximately 17:00 also for the next day. A new strategic cycle fires
automatically when prices update.

---

### Two Optimization Modes

Set `USE_LP_OPTIMIZER` at the top of the script to choose between them.

#### Linear Program (USE_LP_OPTIMIZER = True) — recommended

Solves a Linear Program via `scipy` HiGHS across all 96 slots simultaneously.
Finds the globally cost-optimal charge/discharge schedule given forecast
consumption, solar production, and spot prices. Solve time is ~50–200 ms.

The LP minimizes total grid import cost:

```
minimize  Σ  grid_import[t] × price[t] × 0.25h
```

subject to battery energy bounds, inverter power limits, and no-export
constraints. The result is a 96-slot setpoint list; slot 0 is applied
immediately and the full schedule is used for the 24h outlook log.

#### Heuristic (USE_LP_OPTIMIZER = False)

Uses dynamic P25/P75 price percentile thresholds with forward-looking battery
reservation for mid-price periods:

- **Price ≤ P25:** charge from grid
- **Price ≥ P75:** discharge to cover load
- **Mid price:** assess whether upcoming expensive windows and PV recharge
  forecast justify holding the battery or discharging freely

Falls back automatically if the LP solver fails.

---

### Operating Modes

| Mode | Condition | Action |
|---|---|---|
| `⚡ GRID_CHARGE` | Price ≤ P25 and SOC not full | Charge battery from grid at maximum rate |
| `🔋 DISCHARGE` | Price ≥ P75 | Discharge to cover apartment load — never export |
| `🌿 TRICKLE` | SOC between 96–98% | Hysteresis control — hold battery near full |
| `⚖️ BALANCE` | All other conditions | Tactical layer follows grid in real time |

---

### Trickle Hysteresis (Sunny Day Handling)

When the battery approaches full on a sunny day, a hysteresis band between
`BATTERY_TRICKLE_PCT` (96%) and `BATTERY_FULL_PCT` (98%) prevents the
inverter from curtailing PV production:

```
SOC ≥ 98%  →  gently increase inverter output (+10 W steps) to divert
               PV power to load and prevent curtailment
SOC 96–98% →  hold or gently recharge (-10 W steps)
SOC < 96%  →  exit trickle, return to BALANCE
```

If PV surplus genuinely exceeds all load even at full battery, the optimizer
spills exactly the surplus to the grid — the minimum needed to prevent
curtailment. **Energy is never deliberately exported for profit** since there
is no feed-in tariff.

---

### Tactical Layer — every 5 seconds

A proportional real-time controller that targets `grid_power ≈ 0`:

```
new_setpoint = current_setpoint + RT_GAIN × grid_power
```

- `grid_power > 0` (importing) → increase inverter output → battery discharges more
- `grid_power < 0` (exporting) → decrease inverter output → battery charges from PV

The tactical layer only runs its proportional logic in `BALANCE` mode. In
`GRID_CHARGE` and `DISCHARGE` modes it re-asserts the strategic setpoint every
5 seconds to guard against inverter resets. In `TRICKLE` mode it nudges the
setpoint in ±10 W steps based on live SOC and battery power readings.

---

## Dashboard Card

The optimizer writes its current mode and reasoning to two `input_text` helpers
that can be displayed on any dashboard.

Add a **Markdown card**:

```yaml
type: markdown
title: Energy Optimizer
content: >
  ## {{ states('input_text.energy_optimizer_mode') }}

  {{ states('input_text.energy_optimizer_reason') }}

  ---
  **SOC:** {{ states('sensor.ezhi_battery_state_of_charge') }}%
  | **Grid:** {{ states('sensor.shrdzm_485519e15aae_16_7_0') | int }}W
  | **Price:** {{ state_attr('sensor.epex_spot_data_total_price', 'data')[0].price_per_kwh | round(4) }} €/kWh
```

---

## 24h Outlook Log

Once per hour (on the first 15-min cycle of each hour), the optimizer logs a
human-readable 24h operational plan to the HA log. Consecutive slots with the
same planned action are merged into labelled time windows:

```
─── 24h Outlook (LP) | SOC 72% | P25=0.1201 P75=0.1876 €/kWh ───
  11:00–12:30 ( 90min)  ⚖️  Follow grid / self-consume    0.1423 €/kWh    avg net load +120W
  12:30–15:45 (195min)  ☀️  PV surplus / spill             0.1312 €/kWh    avg 340W surplus
  15:45–17:00 ( 75min)  ⚡ Charge from grid                0.1198 €/kWh    avg -1200W from grid
  17:00–18:15 ( 75min)  ⚖️  Follow grid / self-consume    0.1534 €/kWh    avg net load +280W
  18:15–22:00 (225min)  🔋 Discharge (peak price)          0.2134 €/kWh    avg +480W output
  22:00–00:00 (120min)  ⚖️  Follow grid / self-consume    0.1623 €/kWh    avg net load +190W
  00:00–06:00 (360min)  ⚡ Charge from grid                0.1089 €/kWh    avg -1200W from grid
────────────────────────────────────────────────────────────────
```

Filter for `energy_optimizer` in **Settings → System → Logs** to read it, or
tail live via terminal:

```bash
tail -f /config/home-assistant.log | grep energy_optimizer
```

---

## User-Configurable Constants

All tuneable parameters are defined at the top of `energy_optimizer.py`.

### Optimization Mode

| Constant | Default | Description |
|---|---|---|
| `USE_LP_OPTIMIZER` | `True` | `True` = Linear Program (scipy HiGHS) · `False` = Heuristic P25/P75 |

### Battery and Inverter

| Constant | Default | Description |
|---|---|---|
| `BATTERY_SIZE_WH` | `2760` | Battery capacity in Wh |
| `OUTPUT_MIN_W` | `-1200` | Minimum inverter setpoint in W (negative = grid charging) |
| `OUTPUT_MAX_W` | `1200` | Maximum inverter setpoint in W (positive = discharge) |
| `BATTERY_FULL_PCT` | `98` | SOC % considered full — triggers trickle mode |
| `BATTERY_TRICKLE_PCT` | `96` | Lower bound of trickle hysteresis band in % |
| `BATTERY_EMPTY_PCT` | `15` | SOC % considered empty — triggers grid charge if price is low |
| `BATTERY_TRICKLE_W` | `10` | Step size in W for trickle nudges |
| `GRID_DEADZONE_W` | `10` | Grid power within ±N watts is considered balanced (no action) |

### Real-Time Controller

| Constant | Default | Description |
|---|---|---|
| `RT_GAIN` | `0.8` | Proportional gain — fraction of grid error applied per 5-sec step. Lower = smoother but slower. Raise if grid oscillates, lower if it overshoots. |
| `RT_ROUND_W` | `10` | Setpoint rounded to nearest N watts to reduce inverter chatter |
| `RT_MIN_DELTA` | `10` | Minimum W change before a new setpoint is written to HA. Prevents flooding the inverter API. |

### InfluxDB

| Constant | Default | Description |
|---|---|---|
| `INFLUX_URL` | `http://localhost:8086/query` | InfluxDB HTTP endpoint |
| `INFLUX_DB` | `homeassistant` | Database name |
| `INFLUX_USER` | `homeassistant` | Username |
| `INFLUX_PASS` | `hainflux!` | Password |
| `INFLUX_ENTITY` | `total_consumption` | Entity ID stored in InfluxDB |
| `INFLUX_UNIT` | `W` | InfluxDB measurement name — change to `kW` if sensor reports in kW |

### Behaviour Flags

| Constant | Default | Description |
|---|---|---|
| `ALLOW_EXPORT` | `False` | Set to `True` if you have a feed-in tariff and want deliberate export during peak prices |

### Timezone

| Constant | Default | Description |
|---|---|---|
| `TZ` | `Europe/Vienna` | Local timezone for all scheduling and InfluxDB queries |

---

## Entities Used

| Entity | Description |
|---|---|
| `sensor.shrdzm_485519e15aae_16_7_0` | Grid power (W) — positive = import, negative = export |
| `sensor.ezhi_battery_state_of_charge` | Battery SOC (0–100%) |
| `sensor.ezhi_battery_power` | Battery power (W) — positive = charging, negative = discharging |
| `number.apsystems_ezhi_max_output_power` | Inverter output setpoint (W) — written by optimizer |
| `sensor.epex_spot_data_total_price` | EPEX Spot price data with 15-min intervals |
| `sensor.solcast_pv_forecast_forecast_next_hour` | Solcast hourly solar forecast |
| `sensor.solcast_pv_forecast_forecast_tomorrow` | Solcast total forecast for tomorrow |
| `input_text.energy_optimizer_mode` | Current operating mode (written by optimizer) |
| `input_text.energy_optimizer_reason` | Human-readable reasoning for current mode |

---

## Manual Controls

### Force a strategic cycle immediately

In **Developer Tools → Actions**, call:

```
pyscript.energy_optimizer_force_run
```

Useful after changing constants or to immediately re-evaluate after a manual
battery charge or price update.

### Reload after editing the script

**Settings → Integrations → pyscript → three dots → Reload**

No full HA restart needed for script edits after the initial installation.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `No EPEX price data available` | Wrong entity ID | Check entity ID in Developer Tools → Template |
| `No InfluxDB data — using fallback` | Wrong entity or measurement name | Verify `INFLUX_ENTITY` and `INFLUX_UNIT` match your InfluxDB schema |
| `LP solver status 2` | Infeasible LP (battery constraints too tight) | Check `BATTERY_EMPTY_PCT` / `BATTERY_FULL_PCT` are realistic |
| `scipy/numpy import failed` | scipy not available | Set `USE_LP_OPTIMIZER = False` to use heuristic instead |
| `not implemented ast ast_generatorexp` | Generator expression in pyscript | Replace all `(x for x in y)` patterns with explicit `for` loops |
| Battery discharging when it shouldn't | Mode DISCHARGE setpoint too aggressive | Lower `RT_GAIN` or check P75 threshold logic |
| Inverter API flooded with calls | `RT_MIN_DELTA` too low | Raise `RT_MIN_DELTA` to `25` or `50` W |
| Mode/reason card blank on dashboard | `input_text` helpers not added to config | Add helpers to `configuration.yaml` and do a full HA restart |
