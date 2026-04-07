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
