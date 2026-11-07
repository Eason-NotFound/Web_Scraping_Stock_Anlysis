# NewsLens · ECON3086

NewsLens collects stock/news observations, validates evidence-backed AI interpretations, and presents saved results in English or Chinese. It does not place trades.

## Interfaces

- **Local Streamlit dashboard**: `app.py`, with replay, live monitoring and operator-only rule editing.
- **Public web dashboard**: `frontend/public/`, a read-only static website for Cloudflare Pages.
- **Local read-only API**: `newslens/api.py`, exposing five authenticated JSON endpoints to the Pages server-side proxy.
- **Independent monitor**: `newslens/worker.py`, which collects data and analyses news into the same local SQLite database.

Public data flow:

`Browser → Pages /api/* proxy → Cloudflare Tunnel → local API → SQLite ← local monitor`

The API token stays in server configuration. Alpaca and HKBU credentials remain on the local machine. Browsers cannot start the monitor, change rules or retrieve keys.

## Run locally

Use Python 3.12 and Node.js 22 or later. From the project root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-api.txt
```

For a new checkout only, copy `.env.example` to `.env` and configure your own provider credentials. Do not overwrite an existing `.env`.

```bash
.venv/bin/python run_api.py --init
./Start_API.command
```

In another terminal:

```bash
cd frontend
npm ci
npm run build
npx wrangler pages dev dist --ip 127.0.0.1 --port 8788
```

Open `http://127.0.0.1:8788`. The API listens on loopback port 8000 and requires a Bearer token; the local proxy loads its private settings from `frontend/.dev.vars`. Neither this file nor `.env.api` belongs in Git.

If the website loses its connection after sleep or a network change, run `./Reconnect_Website.command` on the configured Mac. It checks the local API, replaces an expired Quick Tunnel, updates encrypted Pages configuration and republishes through GitHub Actions. It preserves the running news monitor. Use `.venv/bin/python reconnect_web.py --check` for a read-only status check. Always open `https://newslens-gp.pages.dev`; historical deployment URLs retain their original connection settings.

To collect new prices and analyses, run `./Start_Monitor.command` in a separate terminal. To use the local Streamlit interface, run `./Start_Dashboard.command`. Stop each process with Control+C. The public dashboard accurately shows paused/offline states and source timestamps; it never substitutes demo records for missing live data.

## Cloudflare and GitHub Actions

The repository includes a Direct Upload Pages deployment workflow:

`push main → Python and JavaScript tests → static build → Wrangler deploy`

Set these GitHub repository variables:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_PROJECT_NAME`

Set the repository secret `CLOUDFLARE_API_TOKEN` with Cloudflare Pages Edit permission for the target account. Configure both Pages runtime `NEWSLENS_API_BASE` (the HTTPS Tunnel origin) and `NEWSLENS_API_TOKEN` as encrypted Secrets separately. Wrangler preserves these secrets across deployments; dashboard-only plain-text variables are removed when this configuration file is deployed.

Without those settings the workflow explicitly skips deployment; a passing test run does not mean the site is online. Do not enable a second native Git auto-deployment for the same Direct Upload project.

See `DEPLOY_CLOUDFLARE_CN.txt` for account creation, the free `pages.dev` address, temporary Quick Tunnel setup, runtime secrets and deployment verification. A custom domain is optional for the Pages frontend; a stable named Tunnel hostname requires a domain. Quick Tunnel URLs change on restart.

The Mac must remain awake and connected for fresh data. GitHub Actions publishes frontend code; it does not update or restart the Mac's backend. New market/news records update through API polling, not commits.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
cd frontend
npm test
npm run build
```

Current local verification: 101 Python tests and 26 JavaScript tests. Coverage includes mode isolation, evidence checks, signal rules, bilingual state preservation, read-only API authentication and projection, pagination, proxy restrictions and refresh behavior. Live provider connectivity has been checked locally; model semantic accuracy remains a separate human-review task.

## Data and source boundaries

Do not commit `.env`, `.env.api`, `.dev.vars`, databases, runtime logs, backups, dependency folders or temporary files. The API provides only `/api/status`, `/api/quotes`, `/api/bars`, `/api/news` and `/api/alerts`; it does not expose database files, internal logs or operator controls.

News source text, AI explanations and evidence remain in their original language. Source timestamps, feed coverage and incomplete/failed analysis states are retained. API responses are uncached and bounded; UI refresh intervals are scheduling targets, not guarantees of market-data freshness.

M0–M4 frozen archives and subsequent local milestone records remain in the canonical GP project. This repository contains the current runnable source and deployment configuration; it excludes local runtime data and archival bundles.
