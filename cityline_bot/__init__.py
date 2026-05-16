"""
Cityline 智能购票系统
模块化重构版本
"""

try:
    from .core.ticket_purchaser import SmartTicketPurchaser
except ModuleNotFoundError:
    SmartTicketPurchaser = None

try:
    from .config.settings import Settings
except ModuleNotFoundError:
    Settings = None

__version__ = "2.0.0"
__all__ = ["SmartTicketPurchaser", "Settings"]
