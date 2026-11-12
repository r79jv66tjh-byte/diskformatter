"""Einstellungen (%APPDATA%\\DiskFormatter\\settings.json).

``allow_internal`` (interne Platten freischalten) lässt sich nur in der GUI ändern – die CLI
liest den Wert nur. Für Tests kann der Pfad per Umgebungsvariable DISKFORMATTER_SETTINGS
umgelenkt werden.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def settings_path() -> Path:
    env = os.environ.get("DISKFORMATTER_SETTINGS")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "DiskFormatter" / "settings.json"


@dataclass
class Settings:
    allow_internal: bool = False
    verify_samples: int = 256

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        s = cls()
        s.allow_internal = data.get("allow_internal") is True
        try:
            s.verify_samples = max(0, min(10000, int(data.get("verify_samples", s.verify_samples))))
        except (TypeError, ValueError):
            pass
        return s

    def save(self) -> None:
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
