# main.py
"""
Pyscript entry point – wires the three layers together.
All heavy lifting lives in the imported modules.
"""

from . import data_fetcher as df
from . import optimizer    as opt
from . import output_handler as out

# pyscript globals that are available in the module scope:
#   state, input_number, input_text, persistent_notification, log,
#   @time_trigger, @state_trigger, @service
# They are used directly in the functions below.

USE_LP_OPTIMIZER = CONFIG.general.use_lp_optimizer   # read from yaml


# ----------------------------------------------------------------------
# Strategic layer (runs every 15 min and on price update)
# ----------------------------------------------------------------------
@time_trigger("cron(0,15,30,45 * * * *)")
async def strategic_optimize():
    log.info(f"── Strategic cycle ({'LP' if USE_LP_OPTIMIZER else 'Heuristic'}) ──")
    try:
        # ----- 1️⃣ Data layer -----
        soc_raw = state.get(CONFIG.influx.entity_soc)
        if soc_raw in (None, "unavailable", "unknown"):
            log.warning("Battery SOC unavailable — skipping")
            return
        soc = float(soc_raw)

        consumption = await df.fetch_historical_consumption()
        actuals     = await df.fetch_solar_actuals()
        # we need a way to query state for Solcast entities inside data_fetcher;
        # we pass a lambda that forwards to the pyscript `state` object.
        solar = df.blend_solar_forecast(actuals, lambda entity: state.getattr(entity) or {})
        prices = await df.fetch_spot_prices()

        if CONFIG.general.log_debug and prices:
            log.info("── EPEX prices (incl. network fee) ──")
            for (h, q), p in sorted(prices.items()):
                log.info(f"  {h:02d}:{q*15:02d}  {p*100:.3f} ct/kWh")
            log.info("─────────────────────────────────────")

        if not prices:
            log.warning("No EPEX price data — mode unchanged")
            return

        # ----- 2️⃣ Optimization layer -----
        schedule = opt.build_schedule(consumption, solar, prices)
        optimal_schedule = opt.get_optimal_setpoints(soc, schedule, use_lp=USE_LP_OPTIMIZER)

        # ----- 3️⃣ Output layer (first slot determines the immediate command) -----
        raw_sp = optimal_schedule[0] if optimal_schedule else 0
        net    = schedule[0]["net"]   if schedule else 0.0
        price  = schedule[0]["price"] if schedule else 0.15

        # p25 / p75 for reasoning – in a real version these would come from the optimizer’s internal context
        p25 = 0.10
        p75 = 0.20
        mode, sp = opt.apply_trickle_override(soc, raw_sp, net, price, p25, p75)

        out.write_ha_outputs(mode, sp)

        # ----- status text -----
        if mode == "GRID_CHARGE":
            reason = (
                f"Price {price*100:.1f} ct/kWh is in cheapest 25% "
                f"(≤ {p25*100:.1f} ct). "
                f"Charging battery at max rate ({CONFIG.battery.output_min_w}W). SOC: {soc:.0f}%."
            )
        elif mode == "DISCHARGE":
            reason = (
                f"Price {price*100:.1f} ct/kWh is in most expensive 25% "
                f"(≥ {p75*100:.1f} ct). "
                f"Discharging battery ({sp:+d}W). SOC: {soc:.0f}%."
            )
        elif mode == "TRICKLE" and soc >= CONFIG.battery.full_pct:
            reason = (
                f"Battery full ({soc:.0f}%). Holding at 0W. "
                f"PV covers load, battery floats. "
                f"Price: {price*100:.1f} ct/kWh."
            )
        elif mode == "TRICKLE":
            reason = (
                f"SOC {soc:.0f}% in hysteresis band "
                f"({CONFIG.battery.trickle_pct}–{CONFIG.battery.full_pct}%). "
                f"Gently recharging. Price: {price*100:.1f} ct/kWh."
            )
        elif mode == "BALANCE" and soc >= CONFIG.battery.full_pct and net < -CONFIG.grid.grid_deadzone_w:
            reason = (
                f"Battery full ({soc:.0f}%) with PV surplus {abs(net):.0f}W. "
                f"Price: {price*100:.1f} ct/kWh."
            )
        else:
            if USE_LP_OPTIMIZER:
                reason = (
                    f"LP: no strong signal at {price*100:.1f} ct/kWh "
                    f"[P25={p25*100:.1f} P75={p75*100:.1f} ct]. "
                    f"Grid consumption. SOC: {soc:.0f}%. Slot-0: {raw_sp:+d}W."
                )
            else:
                # placeholder – you could call the heuristic helpers here
                reason = (
                    f"Mid price ({price*100:.1f} ct). Holding for expensive window "
                    f"PV insufficient. SOC: {soc:.0f}%."
                )

        out.update_status(mode, reason)

        log.info(
            f"Mode={mode} | SOC={soc:.0f}% | "
            f"Price={price*100:.1f} ct | "
            f"Optimizer={raw_sp:+d}W → Applied={sp:+d}W"
        )

        # ----- 4️⃣ Outlook & logging -----
        out.log_24h_outlook(schedule, optimal_schedule, soc, use_lp=USE_LP_OPTIMIZER)

    except Exception as exc:
        import traceback
        log.error(f"Strategic error: {exc}\n{traceback.format_exc()}")


# ----------------------------------------------------------------------
# Event triggers
# ----------------------------------------------------------------------
@state_trigger(CONFIG.influx.price_sensor)
async def on_price_update(**kwargs):
    log.info("EPEX price data updated — triggering strategic cycle")
    await strategic_optimize()


@state_trigger(CONFIG.influx.entity_soc)
def on_soc_critical(**kwargs):
    soc_raw = state.get(CONFIG.influx.entity_soc)
    if soc_raw in (None, "unavailable", "unknown"):
        return
    if float(soc_raw) < 12:                     # emergency threshold – could also be yaml
        input_number.set_value(entity_id="input_number.energy_optimizer_mode_id",  value=0)
        input_number.set_value(entity_id="input_number.energy_optimizer_setpoint", value=0)
        out.update_status(
            "BALANCE",
            f"⚠️ Emergency: SOC critically low ({soc_raw}%). Inverter forced to 0W.",
        )
        persistent_notification.create(
            title="⚠️ Battery Critical",
            message=f"SOC is {soc_raw}% — inverter forced to 0 W.",
            notification_id="energy_optimizer_critical",
        )
        log.warning(f"Battery critical ({soc_raw}%) — forced BALANCE, setpoint 0W")


# ----------------------------------------------------------------------
# Manual service
# ----------------------------------------------------------------------
@service
async def energy_optimizer_force_run():
    """Callable via Developer Tools → Actions → pyscript.energy_optimizer_force_run"""
    log.info("Manual trigger — running strategic cycle now")
    await strategic_optimize()
