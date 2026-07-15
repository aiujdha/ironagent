from src.market.service import MarketService
from src.market.skill.models import SkillRecord


def install_skill(record: SkillRecord, assistant_id: str) -> dict[str, str]:
    result = MarketService().install_skill(record.id, assistant_id)
    return {
        "assistant_id": result.assistant_id,
        "skill": record.name,
        "status": "installed",
    }
