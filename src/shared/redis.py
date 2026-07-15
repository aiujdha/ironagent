from dataclasses import dataclass

from src.shared.config import get_settings


@dataclass(slots=True)
class RedisState:
    url: str


def get_redis_state() -> RedisState:
    settings = get_settings()
    return RedisState(url=settings.redis_url)
