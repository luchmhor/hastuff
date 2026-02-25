# appdaemon/apps/energy_optimizer.py
"""
EnergyOptimizer — AppDaemon app for Home Assistant
Minimizes electricity cost by controlling APsystems EzHi inverter output
based on historical apartment load, Solcast solar forecast, and EPEX spot prices.

Cycle: every 15 minutes
Horizon: 96 slots (24 h)
"""

import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timedelta
import pytz
import statistics

try:
    from influxdb import InfluxDBClient
    INFLUX_AVAILABLE = True
except ImportError:
    INFLUX_AVAILABLE = False


class EnergyOptimizer(hass.Hass):

    TZ = pytz.timezone("Europe/Vienna")

    # ── Hardware constants ──────────────────────────────────────────────
    BATTERY_SIZE_WH   = 2760
    OUTPUT_MIN_W      = -1200    # negative = pull from grid (charge battery)
    OUTPUT_MAX_W      =  1200    # positive = push to grid/home (discharge battery)
    BATTERY_FULL_PCT  =  98
    BATTERY_EMPTY_PCT =  15
    GRID_DEADZONE_W   =  10

    # ── HA entity IDs ────────────────────────────────────────────────────
    E_GRID_POWER     = "sensor.shrdzm_485519e15aae_16_7_0"
    E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
    E_BATTERY_POWER  = "sensor.ezhi_battery_power"
    E_INV_OUTPUT     = "number.apsystems_ezhi_max_output_power"
    E_PRICE_DATA     = "sensor.epex_spot_data_price"
    E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
    E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

    # ── InfluxDB ──────────────────────────────────────────────────────────
    INFLUX_HOST   = "localhost"
    INFLUX_PORT   = 8086
    INFLUX_DB     = "homeassistant"
    INFLUX_USER   = "homeassistant"
    INFLUX_PASS   = "hainflux!"
    INFLUX_ENTITY = "total_consumption"
    # InfluxDB measurement name = HA unit of measurement.
    # Change to "kW" if your total_consumption sensor reports in kW.
    INFLUX_UNIT   = "W"

    INTERVAL_SEC  = 15 * 60     # run every 15 minutes
    HORIZON_SLOTS = 96          # 24 h × 4 slots/h

    # ════════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ════════════════════════════════════════════════════════════════════

    def initialize(self):
        self.log("EnergyOptimizer ▶ starting up")

        self._influx = None
        if INFLUX_AVAILABLE:
            try:
                self._influx = InfluxDBClient(
                    host=self.INFLUX_HOST,
                    port=self.INFLUX_PORT,
                    database=self.INFLUX_DB,
                    username=self.INFLUX_USER,
                    password=self.INFLUX_PASS,
                )
                self._influx.ping()
                self.log("InfluxDB ✓ connected")
            except Exception as exc:
                self.log(
                    f"InfluxDB connection failed ({exc}) — fallback profile active",
                    level="WARNING",
                )
                self._influx = None
        else:
            self.log("influxdb-python not installed — fallback profile active", level="WARNING")

        # Register the 15-min cycle. "now" fires the first run immediately.
        self.run_every(self._run_cycle, "now", self.INTERVAL_SEC)
        self.log("EnergyOptimizer ✓ initialized")

    # ════════════════════════════════════════════════════════════════════
    # MAIN CYCLE
    # ════════════════════════════════════════════════════════════════════

    def _run_cycle(self, kwargs):
        self.log("── optimization cycle start ──")
        try:
            soc = self._read_soc()
            if soc is None:
                self.log("Battery SOC unavailable — skipping cycle", level="WARNING")
                return

            consumption = self._get_historical_consumption()
            solar       = self._get_solar_forecast()
            prices      = self._get_spot_prices()

            if not prices:
                self.log("No EPEX price data available — skipping cycle", level="WARNING")
                return

            schedule = self._build_schedule(consumption, solar, prices)
            setpoint = self._dispatch(soc, schedule)
            self._apply_setpoint(setpoint)

        except Exception as exc:
            import traceback
            self.log(
                f"Cycle error: {exc}\n{traceback.format_exc()}",
                level="ERROR",
            )

    # ════════════════════════════════════════════════════════════════════
    # DATA RETRIEVAL
    # ════════════════════════════════════════════════════════════════════

    def _read_soc(self):
        raw = self.get_state(self.E_BATTERY_SOC)
        if raw in (None, "unavailable", "unknown"):
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # ── 1. Historical consumption (InfluxDB) ─────────────────────────

    def _get_historical_consumption(self):
        """
        Query InfluxDB for the past 4 occurrences of today's weekday (full day each).
        Returns {(hour, quarter_idx): mean_watts} — 96 keys max.

        InfluxDB stores HA data with:
          measurement = unit of measurement (e.g. "W")
          tag entity_id = HA entity ID
          field value   = sensor reading
        """
        if self._influx is None:
            return self._fallback_consumption()

        now   = datetime.now(self.TZ)
        accum = {}  # {(h, q): [watt_samples]}

        for week_back in range(1, 5):
            anchor    = now - timedelta(weeks=week_back)
            day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = day_start + timedelta(days=1)

            start_utc = day_start.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_utc   = day_end.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            query = (
                f'SELECT mean("value") FROM "{self.INFLUX_UNIT}" '
                f'WHERE "entity_id" = \'{self.INFLUX_ENTITY}\' '
                f"AND time >= '{start_utc}' AND time < '{end_utc}' "
                f"GROUP BY time(15m) fill(previous)"
            )
            try:
                for pt in self._influx.query(query).get_points():
                    if pt.get("mean") is None:
                        continue
                    t_local = datetime.fromisoformat(
                        pt["time"].replace("Z", "+00:00")
                    ).astimezone(self.TZ)
                    key = (t_local.hour, t_local.minute // 15)
                    accum.setdefault(key, []).append(pt["mean"])
            except Exception as exc:
                self.log(
                    f"InfluxDB query error (week -{week_back}): {exc}",
                    level="ERROR",
                )

        if not accum:
            self.log("No InfluxDB data returned — using fallback profile", level="WARNING")
            return self._fallback_consumption()

        result = {k: statistics.mean(v) for k, v in accum.items()}
        self.log(
            f"Consumption profile loaded: {len(result)} slots from "
            f"{sum(len(v) for v in accum.values())} data points"
        )
        return result

    @staticmethod
    def _fallback_consumption():
        """Rough 24 h load curve (W) used when InfluxDB is unreachable."""
        hourly = (
            [150] * 6 +   # 00–06  night
            [600] * 3 +   # 06–09  morning peak
            [350] * 8 +   # 09–17  daytime
            [700] * 5 +   # 17–22  evening peak
            [300] * 2     # 22–24  late evening
        )
        return {(h, q): hourly[h] for h in range(24) for q in range(4)}

    # ── 2. Solar forecast (Solcast) ──────────────────────────────────

    def _get_solar_forecast(self):
        """
        Returns {hour: mean_watts} for the next 24 hours.

        Preferred path: `forecast` attribute list from the Solcast integration.
          Each entry: {"period_start": "<ISO>", "pv_estimate": <kW mean over 30 min>}
          Entries are 30-min blocks; we accumulate into hourly buckets → divide by 4
          at schedule-build time to get average W per 15-min slot.

        Fallback: scalar state of sensor.solcast_pv_forecast_forecast_next_hour
          The BJReplay Solcast integration reports this in Wh for the next 60 min.
        """
        solar = {}
        try:
            state = self.get_state(self.E_SOLAR_HOUR, attribute="all") or {}
            attrs = state.get("attributes", {})

            # Try known attribute names across Solcast integration versions
            forecast_list = (
                attrs.get("forecast") or
                attrs.get("detailedForecast") or
                attrs.get("DetailedForecast") or
                attrs.get("forecasts") or
                []
            )

            if forecast_list:
                for entry in forecast_list:
                    t_str = (
                        entry.get("period_start") or
                        entry.get("PeriodStart") or
                        entry.get("period")
                    )
                    pv_kw = float(
                        entry.get("pv_estimate") or
                        entry.get("PvEstimate") or 0
                    )
                    if not t_str:
                        continue
                    t = datetime.fromisoformat(t_str).astimezone(self.TZ)
                    # pv_estimate = mean kW over the 30-min period
                    # Accumulate W per hour (two 30-min blocks per hour)
                    solar[t.hour] = solar.get(t.hour, 0.0) + pv_kw * 1000
                self.log(f"Solar forecast loaded: {len(solar)} hours from attributes")
                return solar

            # Scalar fallback — only covers the next hour
            next_hour_wh = float(state.get("state") or 0)
            now = datetime.now(self.TZ)
            # Wh for 60 min = average W for 60 min
            solar[now.hour] = next_hour_wh
            self.log(
                f"Solcast: no detailed attributes found — scalar fallback "
                f"({next_hour_wh:.0f} Wh next hour only)",
                level="WARNING",
            )

        except Exception as exc:
            self.log(f"Solar forecast error: {exc}", level="WARNING")

        return solar

    # ── 3. EPEX Spot prices ───────────────────────────────────────────

    def _get_spot_prices(self):
        """
        Returns {(hour, quarter): price_eur_per_kwh} from the EPEX sensor.

        The sensor attribute `data` is a list of:
          {start_time: "<ISO>", end_time: "<ISO>", price_per_kwh: <float>}
        Available for today; after ~17:00 also for tomorrow.
        """
        prices = {}
        try:
            state = self.get_state(self.E_PRICE_DATA, attribute="all") or {}
            data  = state.get("attributes", {}).get("data", [])
            for entry in data:
                t = datetime.fromisoformat(entry["start_time"]).astimezone(self.TZ)
                prices[(t.hour, t.minute // 15)] = float(entry["price_per_kwh"])
            self.log(f"Prices loaded: {len(prices)} slots")
        except Exception as exc:
            self.log(f"Spot price error: {exc}", level="ERROR")
        return prices

    # ════════════════════════════════════════════════════════════════════
    # SCHEDULE ASSEMBLY
    # ════════════════════════════════════════════════════════════════════

    def _build_schedule(self, consumption, solar, prices):
        """
        Assembles 96 forward-looking slots (current timestamp → +24 h).

        net_load (W) = expected apartment consumption − expected solar output
          > 0  →  battery/grid must supply power
          < 0  →  solar surplus, can charge battery or export

        Solar stored as hourly mean-W; divide by 4 → average W per 15-min slot.
        """
        now      = datetime.now(self.TZ)
        schedule = []

        for i in range(self.HORIZON_SLOTS):
            t    = now + timedelta(minutes=15 * i)
            key  = (t.hour, t.minute // 15)
            c_w  = consumption.get(key, 300.0)
            s_w  = solar.get(t.hour, 0.0) / 4.0  # hourly W → 15-min average W
            p    = prices.get(key, 0.15)           # default 15 ct/kWh if slot unknown

            schedule.append({
                "index":       i,
                "time":        t,
                "consumption": c_w,
                "solar":       s_w,
                "price":       p,
                "net_load":    c_w - s_w,
            })

        return schedule

    # ════════════════════════════════════════════════════════════════════
    # DISPATCH OPTIMIZER  —  greedy rolling-horizon
    # ════════════════════════════════════════════════════════════════════

    def _dispatch(self, soc: float, schedule: list) -> int:
        """
        Returns the integer Watt setpoint for the CURRENT 15-min slot.

        Uses P25 / P75 price percentiles across the full 24 h horizon as
        dynamic thresholds, so the strategy adapts to today's price spread.

        Priority rules (evaluated top-to-bottom):
          1. SOC ≤ BATTERY_EMPTY_PCT
               cheap  → grid-charge  (OUTPUT_MIN_W)
               else   → hold         (0)
          2. SOC ≥ BATTERY_FULL_PCT
               price > 0  → max export   (OUTPUT_MAX_W)
               else       → cover load only
          3. Current price ≥ P75  →  max discharge  (OUTPUT_MAX_W)
          4. Current price ≤ P25  →  max grid-charge (OUTPUT_MIN_W)
          5. Otherwise (self-consumption mode)
               net_load > deadzone & battery has charge  → discharge to meet load
               net_load < −deadzone & battery has room   → absorb solar surplus
               else                                      → hold (0)
        """
        prices_sorted = sorted(s["price"] for s in schedule)
        n   = len(prices_sorted)
        p25 = prices_sorted[max(0, int(n * 0.25) - 1)]
        p75 = prices_sorted[min(n - 1, int(n * 0.75))]

        cur      = schedule[0]
        price    = cur["price"]
        net_load = cur["net_load"]
        solar    = cur["solar"]

        headroom_wh  = max(0.0, (self.BATTERY_FULL_PCT  - soc) / 100 * self.BATTERY_SIZE_WH)
        available_wh = max(0.0, (soc - self.BATTERY_EMPTY_PCT) / 100 * self.BATTERY_SIZE_WH)

        # ── Rule 1: Battery critically empty ────────────────────────────
        if soc <= self.BATTERY_EMPTY_PCT:
            if price <= p25:
                sp  = self.OUTPUT_MIN_W
                why = f"SOC {soc:.0f}% ≤ empty & price cheap → grid charge"
            else:
                sp  = 0
                why = f"SOC {soc:.0f}% ≤ empty, not cheap → hold"

        # ── Rule 2: Battery full ─────────────────────────────────────────
        elif soc >= self.BATTERY_FULL_PCT:
            if price > 0:
                sp  = self.OUTPUT_MAX_W
                why = f"SOC {soc:.0f}% ≥ full & price > 0 → max export"
            else:
                sp  = max(0, int(net_load))
                why = f"SOC {soc:.0f}% ≥ full & price ≤ 0 → cover load only"

        # ── Rule 3: Expensive period → discharge ─────────────────────────
        elif price >= p75:
            sp  = self.OUTPUT_MAX_W
            why = f"Price {price:.4f} ≥ P75 {p75:.4f} → max discharge"

        # ── Rule 4: Cheap period → buy / charge ──────────────────────────
        elif price <= p25:
            sp  = self.OUTPUT_MIN_W
            why = f"Price {price:.4f} ≤ P25 {p25:.4f} → grid charge"

        # ── Rule 5: Mid price → self-consumption ─────────────────────────
        else:
            if net_load > self.GRID_DEADZONE_W and available_wh > 0:
                # Discharge just enough to cover the expected load
                sp = min(self.OUTPUT_MAX_W, int(net_load))
            elif net_load < -self.GRID_DEADZONE_W and headroom_wh > 0:
                # Absorb solar surplus into battery
                sp = max(self.OUTPUT_MIN_W, int(net_load))
            else:
                sp = 0
            why = (
                f"Price {price:.4f} mid → self-consume "
                f"(net {net_load:+.0f} W, avail {available_wh:.0f} Wh)"
            )

        sp = max(self.OUTPUT_MIN_W, min(self.OUTPUT_MAX_W, sp))

        self.log(
            f"SOC={soc:.1f}% | P={price:.4f} €/kWh "
            f"[P25={p25:.4f} P75={p75:.4f}] | "
            f"Net={net_load:+.0f}W Solar={solar:.0f}W | "
            f"⚡ setpoint={sp:+d}W | {why}"
        )
        return sp

    # ════════════════════════════════════════════════════════════════════
    # ACTUATOR
    # ════════════════════════════════════════════════════════════════════

    def _apply_setpoint(self, setpoint: int):
        self.call_service(
            "number/set_value",
            entity_id=self.E_INV_OUTPUT,
            value=setpoint,
        )
        self.log(f"✓ Inverter setpoint applied: {setpoint:+d} W")
