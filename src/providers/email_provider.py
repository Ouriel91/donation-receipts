from typing import Protocol


class EmailProvider(Protocol):
    def fetch_emails(self) -> list[dict]: ...
