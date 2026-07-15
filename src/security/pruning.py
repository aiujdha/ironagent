from src.security.config_loader import load_yaml, pruning_file


def load_pruned_tools() -> set[str]:
    raw = load_yaml(pruning_file())
    return set(raw.get("tools", []))
