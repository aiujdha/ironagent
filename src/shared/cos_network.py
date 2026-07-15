from __future__ import annotations

import logging
import os
from urllib.parse import urlparse


def configure_cos_sdk_logging() -> None:
    logging.getLogger("qcloud_cos").setLevel(logging.WARNING)


def cos_config_proxies() -> dict[str, None]:
    return {"http": None, "https": None}


def ensure_cos_no_proxy(*, endpoint: str, bucket: str, region: str) -> None:
    hosts = {
        f"{bucket}.cos.{region}.myqcloud.com",
        ".myqcloud.com",
    }
    parsed = urlparse(endpoint.strip())
    endpoint_host = parsed.netloc or endpoint.strip()
    if endpoint_host:
        hosts.add(endpoint_host)

    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    values = [item.strip() for item in existing.split(",") if item.strip()]
    seen = {item.lower() for item in values}
    for host in sorted(hosts):
        if host.lower() not in seen:
            values.append(host)
            seen.add(host.lower())
    joined = ",".join(values)
    os.environ["NO_PROXY"] = joined
    os.environ["no_proxy"] = joined
