import json
from pathlib import Path


class HarnessProvider:
    def __init__(self, harness_dir: Path) -> None:
        self.harness_dir = harness_dir

    def fetch_emails(self) -> list[dict]:
        if not self.harness_dir.exists():
            raise FileNotFoundError(f"Harness directory not found: {self.harness_dir}")
        emails = []
        for path in sorted(self.harness_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as f:
                emails.append(json.load(f))
        return emails
