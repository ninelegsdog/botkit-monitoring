from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Settings:
    api_id: int
    api_hash: str
    phone: str
    timeout: int = 20
    bots_dir: Path = Path("/home/deploy")


@dataclass
class Step:
    send: str
    expect: str


@dataclass
class Scenario:
    steps: list[Step] = field(default_factory=list)


def load_settings() -> Settings:
    return Settings(
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        phone=os.environ["TG_PHONE"],
        timeout=int(os.environ.get("E2E_TIMEOUT", "20")),
        bots_dir=Path(os.environ.get("E2E_BOTS_DIR", "/home/deploy")),
    )


def load_scenarios(path) -> dict[str, Scenario]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {
        b: Scenario(steps=[Step(s["send"], s["expect"]) for s in c.get("steps", [])])
        for b, c in data.items()
    }
