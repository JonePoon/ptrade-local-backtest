"""Public API of ptrade 本地回测框架; legacy strategy imports remain compatible."""

from ptrade_sim import DISTRIBUTION_NAME, PRIMARY_DEVELOPER, PROJECT_NAME, __version__
from ptrade_sim.cli import main
from ptrade_sim.runtime import BacktestEngine
from ptrade_sim.strategy_export import convert_strategy, write_export

__all__ = [
    "DISTRIBUTION_NAME",
    "PRIMARY_DEVELOPER",
    "PROJECT_NAME",
    "BacktestEngine",
    "__version__",
    "convert_strategy",
    "main",
    "write_export",
]
