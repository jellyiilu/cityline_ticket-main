"""配置模块"""

try:
    from .constants import Selectors, Keywords, Timeouts, Priorities
except ModuleNotFoundError:
    Selectors = Keywords = Timeouts = Priorities = None

try:
    from .settings import Settings
except ModuleNotFoundError:
    Settings = None

__all__ = ["Selectors", "Keywords", "Timeouts", "Priorities", "Settings"]
