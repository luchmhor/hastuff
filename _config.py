# _config.py
"""
Central configuration loader – reads energy_optimizer_config.yaml once
and makes the values available as attributes (CONFIG.<section>.<key>).
"""
import os
import yaml

# Adjust this path if you place the yaml somewhere else.
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "energy_optimizer_config.yaml")

with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    _raw = yaml.safe_load(f)

# Turn nested dict into a simple object for attribute‑style access.
class _Conf:
    def __init__(self, data):
        for k, v in data.items():
            if isinstance(v, dict):
                setattr(self, k, _Conf(v))
            else:
                setattr(self, k, v)

CONFIG = _Conf(_raw)
