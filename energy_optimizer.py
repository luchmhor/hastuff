"""
Top-level pyscript entry point.

All real logic (data_fetcher, optimizer, output_handler, and the
strategic/event/service functions) lives in the energy_optimizer package.
Importing main ensures its @time_trigger / @state_trigger / @service
decorators are registered with pyscript.
"""

from energy_optimizer.main import (  # noqa: F401
    strategic_optimize,
    on_price_update,
    on_soc_critical,
    energy_optimizer_force_run,
)
