import streamlit as st
import yaml

from src.shared.config import get_settings

st.title("Capability Profiles")

capability_dir = get_settings().config_dir / "capabilities"
paths = sorted(capability_dir.glob("*.yaml"))

if not paths:
    st.info("No capability profiles found.")
else:
    for path in paths:
        with st.expander(path.stem, expanded=path.stem == "onlyread"):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            st.write(
                {
                    "name": payload.get("name"),
                    "level": payload.get("level"),
                    "allowed_tools": payload.get("allowed_tools", []),
                    "approval_tools": payload.get("approval_tools", []),
                }
            )
