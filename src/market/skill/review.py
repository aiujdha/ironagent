from src.market.service import MarketService
from src.market.skill.models import SkillRecord


def approve_skill(record: SkillRecord) -> SkillRecord:
    return MarketService().approve_skill(record.id)
