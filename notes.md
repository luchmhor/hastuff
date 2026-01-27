## Workflow

Create a HA automation using the Entities and Constants below:

# BASIC FUNCIONALITY:

- Rule #1 - follow grid - baseline
  adjust the inverter_output to keep the grid_power within the grid_power_deadzone (plus and minus)

- Rule #2 - Prevent PV Curtailment
  IF the battery is full (battery_full_threshold) AND the grid_power_deadzone is met TRY:
    - increase the inverter_output and keep the battery_power at a discharge level of battery_discharge_curtail.
  # reasoning behind that: the inverter curtails PV production (if we apply rule #1 AND battery is full AND grid_power is already met)


## Entities and Constants

### HA Entities
  # Grid power sensor (positive = import, negative = export)
  grid_power: sensor.shrdzm_485519e15aae_16_7_0
    
  # Battery state of charge percentage (0-100%)
  battery_soc: sensor.ezhi_battery_state_of_charge
    
  # Battery power (positive = charging, negative = discharging)
  battery_power: sensor.ezhi_battery_power
    
  # Inverter output control (adjustable power output with the limits defined below)
  inverter_output: number.apsystems_ezhi_max_output_power
    
  # EPEX Spot price data sensor (15-minute interval pricing)
  price_data: sensor.epex_spot_data_price
  
### Constants
  # Minimum inverter output power in Watts (negative = grid charging)
  output_min_limit: -1200
    
  # Maximum inverter output power in Watts (positive = grid feed-in)
  output_max_limit: 1200

  # Battery SOC percentage considered "full" (triggers special handling)
  battery_full_threshold: 98

  # Battery SOC percentage considered "empty" (triggers special handling)
  battery_empty_threshold: 15

  # Grid power within this range is considered "balanced"
  grid_power_deadzone: 10

  # Battery SOC discharge threshold
  battery_discharge_threshold: -10
