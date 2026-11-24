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
TUNNEL_START_TIMEOUT = 150


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


def saved_data_available(status):
    return status is not None and status.get("data_available") is True


def connection_summary(status):
    if status is None:
        return "unavailable"
    return "connected (saved data readable)" if saved_data_available(status) else "connected (saved data unavailable)"


def monitor_status(status):
    if saved_data_available(status):
        if status.get("worker_running") is True:
            return "running"
        if status.get("worker_running") is False:
            return "paused"
    return "unavailable"


def require_saved_data(status, service):
    if status is not None and not saved_data_available(status):
        raise RuntimeError(f"{service} is reachable, but saved data is unavailable. Check the local API's read access to data/live.sqlite.")


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


def wait_for_tunnel(process, log_path, key):
    deadline = time.monotonic() + TUNNEL_START_TIMEOUT
    base = None
    while process.poll() is None and time.monotonic() < deadline:
        if base is None:
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log_path.read_text(errors="replace"))
            if urls:
                base = urls[-1]
                print("Cloudflare assigned an address. Checking the saved-data API through that address...", flush=True)
        if base:
            status = api_status(base, key)
            if status is not None:
                require_saved_data(status, "Tunnel API")
                return base
        time.sleep(3)
    if process.poll() is not None:
        detail = f"Cloudflare tunnel exited with code {process.returncode} during startup"
    elif base:
        detail = f"Cloudflare assigned an address, but the API was unreachable after {TUNNEL_START_TIMEOUT} seconds"
    else:
        detail = f"Cloudflare did not assign an address within {TUNNEL_START_TIMEOUT} seconds"
    raise RuntimeError(f"{detail}. See work/public_runtime/{log_path.name}.")


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
        print("Local API:", connection_summary(local))
        print("Public website:", connection_summary(public))
        print("Monitor:", monitor_status(local))
        return 0 if saved_data_available(local) and saved_data_available(public) else 1
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
        # The public probe preceded the local API startup.
        public = api_status(PUBLIC)
    require_saved_data(local, "Local API")
    if public is not None:
        require_saved_data(public, "Public website")
        print("Website is connected (saved data readable):", PUBLIC)
        print("Monitor:", monitor_status(local))
        return 0
    connection_path = RUNTIME / "connection.json"
    process_path = RUNTIME / "tunnel_process.json"
    connection = json.loads(connection_path.read_text()) if connection_path.exists() else {}
    old_process = json.loads(process_path.read_text()) if process_path.exists() else {}
    base = connection.get("api_base")
    tunnel_status = api_status(base, key) if tunnel_url(base) else None
    require_saved_data(tunnel_status, "Tunnel API")
    if tunnel_status is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = ROOT / "_integration_backups" / ("web_reconnect_" + stamp)
        backup.mkdir(parents=True)
        for path in (connection_path, process_path):
            if path.exists():
                shutil.copy2(path, backup / path.name)
        log_path = RUNTIME / ("tunnel_" + stamp + ".log")
        with log_path.open("ab") as log:
            p = subprocess.Popen([str(ROOT / "work/tools/cloudflared"), "tunnel", "--url", LOCAL, "--protocol", "http2", "--no-autoupdate"], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        print(f"Creating a new protected Cloudflare connection. Startup can take up to {TUNNEL_START_TIMEOUT} seconds...", flush=True)
        base = None
        try:
            base = wait_for_tunnel(p, log_path, key)
        finally:
            if base is None and p.poll() is None:
                p.terminate()
        write_json(process_path, {"pid": p.pid, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "log_file": str(log_path.relative_to(ROOT))})
        stop_own_tunnel(old_process.get("pid"))
    endpoint = "https://api.cloudflare.com/client/v4/accounts/" + account + "/pages/projects/" + project
    variables = {"NEWSLENS_API_BASE": {"type": "secret_text", "value": base}, "NEWSLENS_API_TOKEN": {"type": "secret_text", "value": key}}
    write_json(connection_path, {"api_base": base, "public_site": PUBLIC, "deployment_pending": True})
    r = requests.patch(endpoint, headers={"Authorization": "Bearer " + cf["CLOUDFLARE_API_TOKEN"]}, json={"deployment_configs": {"production": {"env_vars": variables}}}, timeout=25, allow_redirects=False)
    if r.status_code != 200 or not r.json().get("success"):
        raise RuntimeError("Cloudflare configuration update failed; check the deployment credential.")
    output = gh("workflow", "run", "deploy.yml", "--ref", "main")
    match = re.search(r"https://github.com/[^\s]+/actions/runs/(\d+)", output)
    if not match:
        raise RuntimeError("Workflow requested; check its result in GitHub Actions.")
    run_id = match.group(1)
    print("Publishing the new connection:", match.group(0), flush=True)
    status_warning = False
    for _ in range(90):
        try:
            result = json.loads(gh("run", "view", run_id, "--json", "status,conclusion"))
        except (RuntimeError, ValueError):
            if not status_warning:
                print("GitHub Actions status is temporarily unavailable. Retrying the same deployment...", flush=True)
                status_warning = True
            time.sleep(5)
            continue
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
            require_saved_data(public, "Public website")
            connection = {"api_base": base, "public_site": PUBLIC, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            write_json(connection_path, connection)
            print("Website reconnected (saved data readable):", PUBLIC)
            print("Monitor:", monitor_status(public))
            return 0
        time.sleep(2)
    raise RuntimeError("Deployment completed, but the public API has not recovered yet.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read connection status without changing services or configuration")
    args = parser.parse_args()
    try:
        if args.check:
            return connect(check=True)
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with (RUNTIME / "reconnect.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("A reconnect operation is already running. Keep its terminal open.")
                return 2
            return connect()
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
