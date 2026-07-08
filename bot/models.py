from dataclasses import dataclass


@dataclass(frozen=True)
class Application:
    id: str
    client_name: str
    dealership: str
    status: str
    state: str | None = None
    extra: dict | None = None

    def fingerprint(self) -> str:
        return f"{self.id}|{self.status}|{self.client_name}|{self.dealership}"
