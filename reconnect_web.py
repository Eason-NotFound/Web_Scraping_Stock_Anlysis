"""Restore the public website connection without restarting the news monitor."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

import requests
from run_local import load_env_file

ROOT = Path(__file__).resolve().parent
LOCAL = "http://127.0.0.1:8000"
PUBLIC = "https://newslens-gp.pages.dev"
REPO = "Eason-NotFound/Web_Scraping_Stock_Anlysis"
RUNTIME = ROOT / "work/public_runtime"


def tunnel_url(value):
    return isinstance(value, str) and re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", value) is not None


def api_status(base, token=None):
    # Never follow a redirect with a private token or send it to another host.
    if token and base != LOCAL and not tunnel_url(base):
        raise ValueError("Unsupported protected API origin")
    try:
        headers = {"Authorization": "Bearer " + token} if token else {}
        r = requests.get(base + "/api/status", headers=headers, timeout=8, allow_redirects=False)
        if r.status_code != 200:
            return None
        body = r.json()
        if body.get("meta", {}).get("data_mode") != "live" or not isinstance(body.get("data"), dict):
            return None
        return body["data"]
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return None


def stop_own_tunnel(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return
    p = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    prefix = str(ROOT / "work/tools/cloudflared") + " tunnel --url " + LOCAL
    if p.returncode == 0 and (p.stdout.strip() == prefix or p.stdout.strip().startswith(prefix + " ")):
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def gh(*arguments):
    env = os.environ.copy()
    env["GH_CONFIG_DIR"] = str(ROOT / "work/github-auth")
    env["GH_PROMPT_DISABLED"] = "1"
    p = subprocess.run([str(ROOT / "work/tools/gh"), *arguments, "--repo", REPO], env=env, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("GitHub operation failed. Check GitHub login and Actions.")
    return p.stdout + p.stderr


def connect(check=False):
    key = load_env_file(ROOT / ".env.api").get("NEWSLENS_API_TOKEN")
    cf = load_env_file(ROOT / ".env.cloudflare")
    if not key or not all(cf.get(k) for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_PROJECT_NAME", "CLOUDFLARE_API_TOKEN")):
        raise RuntimeError("Private local API / Cloudflare settings are missing.")
    account, project = cf["CLOUDFLARE_ACCOUNT_ID"], cf["CLOUDFLARE_PROJECT_NAME"]
    if not re.fullmatch(r"[a-f0-9]{32}", account) or project != "newslens-gp":
        raise RuntimeError("Unexpected Cloudflare target; configuration was not changed.")
    local, public = api_status(LOCAL, key), api_status(PUBLIC)
    if check:
        print("Local API:", "connected" if local is not None else "unavailable")
        print("Public website:", "connected" if public is not None else "unavailable")
        print("Monitor:", "running" if local and local.get("worker_running") else "paused / unavailable")
        return 0 if local is not None and public is not None else 1
    if local is None:
        # Only start an API when the port has no listener. Preserve an existing service.
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 8000)) == 0:
                raise RuntimeError("Port 8000 is occupied but API authentication failed. Existing service preserved.")
        with (RUNTIME / "api.log").open("ab") as log:
            p = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-u", str(ROOT / "run_api.py")], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        write_json(RUNTIME / "api_process.json", {"pid": p.pid, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        for _ in range(10):
            local = api_status(LOCAL, key)
            if local is not None:
                break
            time.sleep(1)
        if local is None:
            raise RuntimeError("Local API did not start. See work/public_runtime/api.log.")
    if public is not None:
        print("Website is connected:", PUBLIC)
        print("Monitor:", "running" if local.get("worker_running") else "paused")
        return 0
    connection_path = RUNTIME / "connection.json"
    process_path = RUNTIME / "tunnel_process.json"
    connection = json.loads(connection_path.read_text()) if connection_path.exists() else {}
    old_process = json.loads(process_path.read_text()) if process_path.exists() else {}
    base = connection.get("api_base")
    if not tunnel_url(base) or api_status(base, key) is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = ROOT / "_integration_backups" / ("web_reconnect_" + stamp)
        backup.mkdir(parents=True)
        for path in (connection_path, process_path):
            if path.exists():
                shutil.copy2(path, backup / path.name)
        log_path = RUNTIME / ("tunnel_" + stamp + ".log")
        with log_path.open("ab") as log:
            p = subprocess.Popen([str(ROOT / "work/tools/cloudflared"), "tunnel", "--url", LOCAL, "--protocol", "http2", "--no-autoupdate"], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print("Creating a new protected Cloudflare connection...", flush=True)
        base = None
        for _ in range(45):
            if p.poll() is not None:
                break
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log_path.read_text())
            if urls and api_status(urls[-1], key) is not None:
                base = urls[-1]
                break
            time.sleep(1)
        if base is None:
            p.terminate()
            raise RuntimeError("New connection failed. Inspect the latest tunnel log.")
        write_json(process_path, {"pid": p.pid, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "log_file": str(log_path.relative_to(ROOT))})
        stop_own_tunnel(old_process.get("pid"))
    endpoint = "https://api.cloudflare.com/client/v4/accounts/" + account + "/pages/projects/" + project
    variables = {"NEWSLENS_API_BASE": {"type": "secret_text", "value": base}, "NEWSLENS_API_TOKEN": {"type": "secret_text", "value": key}}
    r = requests.patch(endpoint, headers={"Authorization": "Bearer " + cf["CLOUDFLARE_API_TOKEN"]}, json={"deployment_configs": {"production": {"env_vars": variables}}}, timeout=25, allow_redirects=False)
    if r.status_code != 200 or not r.json().get("success"):
        # Keep the working connection available for another attempt.
        write_json(connection_path, {"api_base": base, "public_site": PUBLIC, "deployment_pending": True})
        raise RuntimeError("Cloudflare configuration update failed; check the deployment credential.")
    write_json(connection_path, {"api_base": base, "public_site": PUBLIC, "deployment_pending": True})
    output = gh("workflow", "run", "deploy.yml", "--ref", "main")
    match = re.search(r"https://github.com/[^\s]+/actions/runs/(\d+)", output)
    if not match:
        raise RuntimeError("Workflow requested; check its result in GitHub Actions.")
    run_id = match.group(1)
    print("Publishing the new connection:", match.group(0), flush=True)
    for _ in range(90):
        result = json.loads(gh("run", "view", run_id, "--json", "status,conclusion"))
        if result["status"] == "completed":
            if result["conclusion"] != "success":
                raise RuntimeError("Deployment failed. See the GitHub Actions link above.")
            break
        time.sleep(5)
    else:
        raise RuntimeError("Deployment is still pending. See the GitHub Actions link above.")
    for _ in range(15):
        public = api_status(PUBLIC)
        if public is not None:
            connection = {"api_base": base, "public_site": PUBLIC, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            write_json(connection_path, connection)
            print("Website reconnected:", PUBLIC)
            print("Monitor:", "running" if public.get("worker_running") else "paused")
            return 0
        time.sleep(2)
    raise RuntimeError("Deployment completed, but the public API has not recovered yet.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read connection status without changing services or configuration")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "reconnect.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A reconnect operation is already running. Keep its terminal open.")
            return 2
        try:
            return connect(args.check)
        except KeyboardInterrupt:
            print("Setup interrupted; check the website or run this command again.")
            return 130
        except RuntimeError as exc:
            print(str(exc))
            return 2
        except Exception:
            # Providers can include sensitive request details in exceptions.
            print("Connection setup did not finish. Check local settings, the latest tunnel log, and GitHub Actions. Secret values are hidden.")
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
