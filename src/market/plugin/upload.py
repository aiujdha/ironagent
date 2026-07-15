from src.market.plugin.models import PluginRecord
from src.market.service import MarketService


def upload_plugin(
    *,
    filename: str,
    content: bytes,
    name: str,
    version: str,
    owner: str = "",
    tags: str = "",
) -> PluginRecord:
    return MarketService().upload_plugin(
        filename=filename,
        content=content,
        slug=name,
        display_name=name,
        owner=owner,
        version=version,
        tags=tags,
    )
