#!/usr/bin/env python3
"""Scan assistant records for provider_api_key configs that lost their credentials.

A record is degraded when ``auth_mode == "provider_api_key"`` but the config can no
longer produce a valid ironagent.json provider block — the api key is empty/masked,
or the provider id is unresolvable (empty ``provider_id`` and a ``primary_model``
without a ``"<provider>/"`` prefix). The provisioner now fails closed on these, and
historically they caused ``authHeader``/``headers``/``models`` to be silently dropped.

The lost API keys cannot be recovered automatically; affected assistants must have
their provider config re-entered via ``PUT /api/v1/admin/assistants/<id>/model-config``.

Usage::

    uv run python scripts/k8s/diagnose_provider_credentials.py

Exit code 1 when degraded records are found, else 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.assistants.service import AssistantService  # noqa: E402
from src.runtime.models import AssistantIronAgentConfig  # noqa: E402
from src.runtime.provisioner import is_placeholder_api_key  # noqa: E402


def degraded_reasons(config: AssistantIronAgentConfig) -> list[str]:
    """Return the reasons a provider_api_key config can no longer be serialized."""
    if config.auth_mode != "provider_api_key":
        return []
    reasons: list[str] = []
    key = config.provider_api_key.strip()
    if not key:
        reasons.append("missing provider_api_key")
    elif is_placeholder_api_key(key):
        reasons.append("placeholder provider_api_key ('***'/'******') — real key lost")
    if not config.provider_id.strip() and "/" not in config.primary_model:
        reasons.append("unresolvable provider_id")
    return reasons


def main() -> int:
    service = AssistantService()
    records = [record for record in service.list_assistants() if not record.deleted]
    degraded = [
        (record, reasons)
        for record in records
        if (reasons := degraded_reasons(record.iron_agent_config))
    ]

    print(f"Scanned {len(records)} assistant record(s) (deleted excluded).")
    if not degraded:
        print("OK: no provider_api_key assistants with degraded credentials.")
        return 0

    print(f"WARNING: {len(degraded)} assistant(s) have degraded provider credentials:")
    for record, reasons in degraded:
        cfg = record.iron_agent_config
        print(
            f"  - assistant_id={record.assistant_id!r} name={record.name!r} "
            f"primary_model={cfg.primary_model!r} provider_id={cfg.provider_id!r} "
            f"issues=[{', '.join(reasons)}]"
        )
    print(
        "\nThese API keys are lost and cannot be recovered automatically. Re-enter the "
        "full provider config via PUT /api/v1/admin/assistants/<id>/model-config."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
