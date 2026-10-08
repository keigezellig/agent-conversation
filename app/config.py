import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    api_token: str = field(default_factory=lambda: os.getenv("API_TOKEN", ""))


settings = Settings()
