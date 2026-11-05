"""Local read-only API launcher. Keys are never sent to the static frontend."""
import argparse
import os
from pathlib import Path
import secrets
from run_local import load_env_file

ROOT = Path(__file__).resolve().parent


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create local API/proxy configuration, without overwriting an existing token")
    parser.add_argument("--check", action="store_true", help="Check local configuration without starting a server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port between 1024 and 65535")
    os.chdir(ROOT)
    path = ROOT / ".env.api"
    try:
        config = load_env_file(path)
    except (ValueError, OSError):
        print("Could not read .env.api; configuration values are hidden.")
        return 2
    token = os.getenv("NEWSLENS_API_TOKEN") or config.get("NEWSLENS_API_TOKEN", "")
    if args.init:
        if not token:
            token = secrets.token_urlsafe(32)
            if path.exists():
                print("Existing .env.api has no usable token; edit it before initialization.")
                return 2
            write_private(path, "NEWSLENS_API_TOKEN=" + token + "\n")
        variables = ROOT / "frontend" / ".dev.vars"
        variables.parent.mkdir(parents=True, exist_ok=True)
        if not variables.exists():
            write_private(variables, "NEWSLENS_API_BASE=http://127.0.0.1:" + str(args.port) + "\nNEWSLENS_API_TOKEN=" + token + "\n")
        print("Local API/proxy configuration is ready. Existing settings preserved; secret values are hidden.")
        return 0
    if not token:
        print("API token is missing. Run .venv/bin/python run_api.py --init first.")
        return 2
    if args.check:
        print("Local API configuration OK. No server or network request started.")
        return 0
    try:
        import uvicorn
        from newslens.api import create_app
        from newslens.config import load_settings
    except ImportError:
        print("Install API dependencies: .venv/bin/python -m pip install -r requirements-api.txt")
        return 2
    symbols = load_settings(ROOT / "config.toml").symbols
    print(f"NewsLens read-only API: http://127.0.0.1:{args.port} | Ctrl+C stops | token hidden", flush=True)
    uvicorn.run(create_app(ROOT / "data" / "live.sqlite", token, symbols), host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
