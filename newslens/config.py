"""Small TOML config; credentials are read from the environment, never committed."""
from dataclasses import dataclass
from pathlib import Path
import os
import tomllib

ROOT = Path(__file__).resolve().parent.parent

@dataclass(frozen=True)
class Settings:
    symbols: list[str]
    quote_interval: int = 60
    news_interval: int = 180
    max_age_seconds: int = 180
    feed: str = "iex"
    db_path: str = "data/demo.sqlite"
    mode: str = "demo"

def load_settings(path="config.toml"):
    p = Path(path)
    if not p.exists():
        p = ROOT / path
    raw = tomllib.loads(p.read_text()) if p.exists() else {}
    mode = os.getenv("NEWSLENS_MODE", raw.get("mode", "demo"))
    if mode not in {"demo", "live"}:
        raise ValueError("Mode must be demo or live")
    symbols = list(dict.fromkeys(str(x).upper() for x in raw.get("symbols", ["AAPL", "MSFT", "NVDA"])))
    if not symbols or len(symbols) > 20 or any(not s.replace(".", "").isalpha() for s in symbols):
        raise ValueError("Configure 1–20 valid stock symbols")
    db_path = os.getenv("NEWSLENS_DB", str(ROOT / "data" / f"{mode}.sqlite"))
    # Prevent accidental demo/live contamination, including a leftover DB override.
    if mode == "live" and Path(db_path).name == "demo.sqlite":
        raise ValueError("Live mode cannot use demo.sqlite")
    return Settings(symbols=symbols, mode=mode, db_path=db_path,
                    quote_interval=max(30, int(raw.get("quote_interval", 60))),
                    news_interval=max(60, int(raw.get("news_interval", 180))),
                    max_age_seconds=max(30, int(raw.get("max_age_seconds", 180))),
                    feed=raw.get("feed", "iex"))
