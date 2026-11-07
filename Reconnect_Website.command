#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
.venv/bin/python -u reconnect_web.py
