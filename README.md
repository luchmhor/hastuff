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
