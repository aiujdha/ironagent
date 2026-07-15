from src.market.service import MarketService
from src.market.skill.models import SkillRecord


def list_skills() -> list[SkillRecord]:
    return MarketService().list_skills()
