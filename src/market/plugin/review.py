from src.market.plugin.models import PluginRecord
from src.market.service import MarketService


def approve_plugin(record: PluginRecord) -> PluginRecord:
    return MarketService().approve_plugin(record.id)
