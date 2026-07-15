from pathlib import Path

from src.assistants.service import DEFAULT_ASSISTANTS, AssistantService
from src.shared.config import get_settings


def main() -> None:
    Path("./data").mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    service = AssistantService()
    if settings.storage_backend == "mysql" and service.get_assistant("demo-main") is None:
        seeded = DEFAULT_ASSISTANTS[0]
        service.upsert_assistant(
            assistant_id=seeded["assistant_id"],
            name=seeded["name"],
            capability_profile="onlyread",
            notes=seeded["notes"],
            provision_runtime=False,
        )
    print("seed complete")


if __name__ == "__main__":
    main()
