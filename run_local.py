"""Launch the local live monitor or dashboard using GP/.env (never shell-evaluated)."""
import argparse
import os
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parent


def load_env_file(path):
    values = {}
    if not Path(path).exists():
        return values
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        try:
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError
            parts = shlex.split(value, comments=True, posix=True)
            if len(parts) > 1:
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid .env entry on line {number}") from None
        values[key] = parts[0] if parts else ""
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["worker", "dashboard"])
    parser.add_argument("--once", action="store_true", help="Worker: fetch/analyze one cycle, then stop")
    parser.add_argument("--check", action="store_true", help="Check local setup only; no API calls or background process")
    args = parser.parse_args(argv)
    if args.once and args.role != "worker":
        parser.error("--once is only for worker")
    os.chdir(ROOT)
    try:
        os.environ.update(load_env_file(ROOT / ".env"))
        os.environ["NEWSLENS_MODE"] = "live"
        os.environ["NEWSLENS_DB"] = str(ROOT / "data" / "live.sqlite")
        from newslens.config import load_settings
        settings = load_settings(ROOT / "config.toml")
    except (OSError, ValueError):
        print("Local configuration could not be loaded. Check .env and config.toml; values are not printed.", flush=True)
        return 2
    if args.role == "worker":
        required = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            print("Missing local configuration: " + ", ".join(missing), flush=True)
            return 2
    print(f"NewsLens LIVE | {len(settings.symbols)} stocks | quotes {settings.quote_interval}s | news {settings.news_interval}s", flush=True)
    print("Database: " + str(ROOT / "data" / "live.sqlite"), flush=True)
    if args.check:
        print("Local setup OK. 未发起网络请求，也未启动监控。", flush=True)
        return 0
    if args.role == "worker":
        print("每轮最多处理 2 篇待分析新闻；有足够正文时会调用学校模型。保持终端打开，按 Ctrl+C 停止。", flush=True)
        from newslens.worker import main as worker_main
        return worker_main(["--console"] + (["--once"] if args.once else []))
    os.execv(sys.executable, [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
                             "--server.address", "127.0.0.1"])


if __name__ == "__main__":
    raise SystemExit(main())
