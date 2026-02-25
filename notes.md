## Workflow

Create a HA automation using the Entities and Constants below:

## BASIC FUNCIONALITY:

1. take the historical data of the apartment consumption: 15min mean of the same day of the past 4 same days of the week, e.g. today is thursday, get 15min values of the past 4 thursdays from the influxdb and create 15min mean values for the day, do this from the current timestamp 24h into the future
2. take the solar forecast for the current day by hourly values and also the total expected production for the next day
3. take the spot prices from the current timestap 24h into the future
4. based on 1, 2, and 3 control the current inverter output (and subsequently the battery charging, discharging) to minimize the total costs.

## Entities and Constants

### HA Entities
  * Grid power sensor (positive = import, negative = export)
  grid_power: sensor.shrdzm_485519e15aae_16_7_0
    
  * Battery state of charge percentage (0-100%)
  battery_soc: sensor.ezhi_battery_state_of_charge
    
  * Battery power (positive = charging, negative = discharging)
  battery_power: sensor.ezhi_battery_power
    
  * Inverter output control (adjustable power output with the limits defined below)
  inverter_output: number.apsystems_ezhi_max_output_power
    
  * EPEX Spot price data sensor
  data in 15-minute interval pricing
  ahead for the current day, after about 5pm also for the next day
  price_data: sensor.epex_spot_data_price
  ```
     - start_time: '2026-01-27T00:00:00+01:00'
       end_time: '2026-01-27T00:15:00+01:00'
       price_per_kwh: 0.165648
     - start_time: '2026-01-27T00:15:00+01:00'
       end_time: '2026-01-27T00:30:00+01:00'
       price_per_kwh: 0.162612
     - start_time: '2026-01-27T00:30:00+01:00'
       end_time: '2026-01-27T00:45:00+01:00'
       price_per_kwh: 0.162768
  ```
  * Appartment Consumption
  past data of the total consumption of the apartment is available through influxdb
  the consumption is essentially grid_power - inverter_output
  ```
  host: localhost
  port: 8086
  database: homeassistant
  username: homeassistant
  password: hainflux!
  entity: total_consumption
  ```

  * Solar Forecast (Solcas):
  ** same day hourly values: `sensor.solcast_pv_forecast_forecast_next_hour`
  ** next day total value: `sensor.solcast_pv_forecast_forecast_tomorrow`
  
### Constants
  * battery capacity (in Wh)
  battery_size: 2760

  * Minimum inverter output power in Watts (negative = grid charging)
  output_min_limit: -1200
    
  * Maximum inverter output power in Watts (positive = grid feed-in)
  output_max_limit: 1200

  * Battery SOC percentage considered "full" (triggers special handling)
  battery_full_threshold: 98

  * Battery SOC percentage considered "empty" (triggers special handling)
  battery_empty_threshold: 15

  * Grid power within this range is considered "balanced"
  grid_power_deadzone: 10

  * Battery SOC discharge threshold
  battery_discharge_threshold: -10
