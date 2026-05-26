import json


def load_accounts(config_path: str) -> list[dict]:
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "accounts" not in data:
        raise ValueError(f"Config missing 'accounts' key: {config_path}")
    return data["accounts"]


def get_enabled_accounts(config_path: str) -> list[dict]:
    accounts = load_accounts(config_path)
    return [a for a in accounts if a.get("enabled", False)]
