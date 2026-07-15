from src.market.service import MarketService
from src.market.skill.models import SkillRecord


def upload_skill(
    *,
    filename: str,
    content: bytes,
    name: str,
    version: str,
    owner: str = "",
    tags: str = "",
) -> SkillRecord:
    return MarketService().upload_skill(
        filename=filename,
        content=content,
        slug=name,
        display_name=name,
        owner=owner,
        version=version,
        tags=tags,
    )
