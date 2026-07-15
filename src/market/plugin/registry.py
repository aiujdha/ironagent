from src.market.plugin.models import PluginRecord
from src.market.service import MarketService


def list_plugins() -> list[PluginRecord]:
    return MarketService().list_plugins()
