from src.market.plugin.models import PluginRecord
from src.market.service import MarketService


def install_plugin(record: PluginRecord, assistant_id: str) -> dict[str, str]:
    result = MarketService().install_plugin(record.id, assistant_id)
    return {
        "assistant_id": result.assistant_id,
        "plugin": record.name,
        "status": "installed",
    }
