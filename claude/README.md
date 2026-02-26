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

### 3. Copy the script

Create the folder `/config/pyscript/` if it does not exist, then copy
`energy_optimizer.py` into it:

```
/config/pyscript/energy_optimizer.py
```

### 4. Restart Home Assistant

A **full restart** is required (not just a pyscript reload) for the first
installation. After that, edits to the script only require a pyscript reload:

**Settings → Integrations → pyscript → three dots → Reload**

### 5. Verify it is running

Go to **Settings → System → Logs** and filter for `energy_optimizer`.
You should see lines like:

```
── Strategic optimization cycle ──
EPEX data slots found: 96
_compute_mode: SOC=72 price=0.1423 p25=0.1201 p75=0.1876 net=+120W
Mode=BALANCE | SOC=72% | Price=0.1423 €/kWh [P25=0.1201 P75=0.1876] | Guidance=+120 W
```

---

## How the Optimization Works

The optimizer runs two control loops simultaneously.

### Strategic Layer — every 15 minutes

Runs a full planning cycle over a 24-hour horizon using three data sources:

**1. Historical consumption (InfluxDB)**
Queries the past 4 occurrences of the same weekday (e.g. today is Thursday →
queries the last 4 Thursdays) and computes a 15-minute mean load profile for
the full day. This gives a realistic forecast of when the apartment will consume
power.

**2. Solar forecast (Solcast)**
Reads hourly PV production estimates from the Solcast integration. Combined with
the consumption profile, this yields a `net_load` per 15-minute slot:

```
net_load = consumption − solar
net_load > 0 → apartment needs power from battery or grid
net_load < 0 → solar surplus available to charge battery or export
```

**3. EPEX Spot prices**
Reads 15-minute electricity prices from the EPEX Spot sensor. Prices are
available for the current day; after approximately 17:00 also for the next day.

Dynamic P25/P75 price percentiles are calculated across the full 24-hour horizon
each cycle. These thresholds adapt to each day's price spread rather than using
fixed price limits.

#### Operating Modes

| Mode | Condition | Action |
|---|---|---|
| `GRID_CHARGE` | Price ≤ P25 and SOC not full | Charge battery from grid at maximum rate |
| `DISCHARGE` | Price ≥ P75 | Discharge battery to cover apartment load (never export) |
| `TRICKLE` | SOC between 96–98% | Hysteresis control — gently hold battery near full |
| `BALANCE` | All other conditions | Tactical layer takes over for real-time self-consumption |

### Tactical Layer — every 5 seconds

A proportional real-time controller that targets `grid_power ≈ 0`:

```
new_setpoint = current_setpoint + GAIN × grid_power
```

- `grid_power > 0` (importing) → increase inverter output (discharge battery more)
- `grid_power < 0` (exporting) → decrease inverter output (let battery charge from PV)

The tactical layer only operates in `BALANCE` mode. In all other modes it simply
re-asserts the strategic setpoint every 5 seconds to guard against inverter resets.

### Trickle Hysteresis (Sunny Day Handling)

When the battery approaches full on a sunny day, a hysteresis band between 96%
and 98% SOC prevents the inverter from curtailing PV production:

```
SOC ≥ 98%  →  gently increase inverter output (+10 W steps) to divert
               PV power to load and prevent curtailment
SOC 96–98% →  hold or gently recharge (-10 W steps)
SOC < 96%  →  exit trickle, return to BALANCE
```

If PV surplus genuinely exceeds all load even with the battery full, the
optimizer spills exactly the surplus amount to the grid — the minimum needed
to prevent curtailment. **Energy is never deliberately exported for profit**
since there is no feed-in tariff.

---

## User-Configurable Constants

All tuneable parameters are defined at the top of `energy_optimizer.py`.

### Battery and Inverter

| Constant | Default | Description |
|---|---|---|
| `BATTERY_SIZE_WH` | `2760` | Battery capacity in Wh |
| `OUTPUT_MIN_W` | `-1200` | Minimum inverter setpoint in W (negative = grid charging) |
| `OUTPUT_MAX_W` | `1200` | Maximum inverter setpoint in W (positive = discharge/export) |
| `BATTERY_FULL_PCT` | `98` | SOC % considered full — triggers trickle mode |
| `BATTERY_TRICKLE_PCT` | `96` | Lower bound of trickle hysteresis band in % |
| `BATTERY_EMPTY_PCT` | `15` | SOC % considered empty — triggers grid charge if price is low |
| `BATTERY_TRICKLE_W` | `10` | Step size in W for trickle nudges |
| `GRID_DEADZONE_W` | `10` | Grid power within ±N watts is considered balanced (no action) |

### Real-Time Controller

| Constant | Default | Description |
|---|---|---|
| `RT_GAIN` | `0.8` | Proportional gain — fraction of grid error applied per 5-sec step. Lower = smoother but slower response. Raise if grid oscillates around zero, lower if it overshoots. |
| `RT_ROUND_W` | `10` | Setpoint is rounded to nearest N watts to reduce inverter chatter |
| `RT_MIN_DELTA` | `10` | Minimum W change before a new setpoint is written to HA. Prevents flooding the inverter API with tiny adjustments. |

### InfluxDB

| Constant | Default | Description |
|---|---|---|
| `INFLUX_URL` | `http://localhost:8086/query` | InfluxDB HTTP endpoint |
| `INFLUX_DB` | `homeassistant` | Database name |
| `INFLUX_USER` | `homeassistant` | Username |
| `INFLUX_PASS` | `hainflux!` | Password |
| `INFLUX_ENTITY` | `total_consumption` | Entity ID stored in InfluxDB |
| `INFLUX_UNIT` | `W` | InfluxDB measurement name — change to `kW` if your sensor reports in kW |

### Behaviour Flags

| Constant | Default | Description |
|---|---|---|
| `ALLOW_EXPORT` | `False` | Set to `True` if you have a feed-in tariff and want to export deliberately during peak price periods |

### Timezone

| Constant | Default | Description |
|---|---|---|
| `TZ` | `Europe/Vienna` | Local timezone used for all scheduling and InfluxDB queries |

---

## Entities Used

| Entity | Description |
|---|---|
| `sensor.shrdzm_485519e15aae_16_7_0` | Grid power (W) — positive = import, negative = export |
| `sensor.ezhi_battery_state_of_charge` | Battery SOC (0–100%) |
| `sensor.ezhi_battery_power` | Battery power (W) — positive = charging, negative = discharging |
| `number.apsystems_ezhi_max_output_power` | Inverter output setpoint (W) — this is what the optimizer writes to |
| `sensor.epex_spot_data_total_price` | EPEX Spot price data with 15-min intervals |
| `sensor.solcast_pv_forecast_forecast_next_hour` | Solcast hourly solar forecast |
| `sensor.solcast_pv_forecast_forecast_tomorrow` | Solcast total forecast for tomorrow |

---

## Manual Controls

### Force a strategic cycle immediately

In **Developer Tools → Actions**, call:

```
pyscript.energy_optimizer_force_run
```

Useful after changing constants or to immediately re-evaluate after a manual
battery charge.

### Reload after editing the script

**Settings → Integrations → pyscript → three dots → Reload**

No full HA restart needed for script edits after the initial installation.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `No EPEX price data available` | Wrong entity ID | Check entity ID with Developer Tools → Template |
| `No InfluxDB data — using fallback` | Wrong entity or measurement name | Verify `INFLUX_ENTITY` and `INFLUX_UNIT` match your InfluxDB schema |
| `not implemented ast ast_generatorexp` | Generator expression in pyscript | Replace all `(x for x in y)` patterns with explicit `for` loops |
| `pyscript functions can't be called from task.executor` | Blocking function wrapped in task.executor | Use `async def` with `aiohttp` instead |
| Battery discharging when it shouldn't | `DISCHARGE` setpoint too aggressive | Lower `RT_GAIN` or narrow the P75 threshold by changing percentile logic |
| Inverter API flooded with calls | `RT_MIN_DELTA` too low | Raise `RT_MIN_DELTA` to `25` or `50` W |
