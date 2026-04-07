# _config.py
"""
Central configuration loader – reads config.yaml once
and makes the values available as attributes (CONFIG.<section>.<key>).
"""
import os
import yaml

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")

with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
    _raw = yaml.safe_load(f)

class _Conf:
    def __init__(self, data):
        for k, v in data.items():
            setattr(self, k, _Conf(v) if isinstance(v, dict) else v)

CONFIG = _Conf(_raw)
