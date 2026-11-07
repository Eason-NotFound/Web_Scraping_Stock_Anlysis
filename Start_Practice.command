#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
.venv/bin/python -u run_practice.py
