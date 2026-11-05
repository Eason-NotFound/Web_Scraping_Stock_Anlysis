#!/bin/zsh
cd -- "${0:A:h}" || exit 1
exec .venv/bin/python -u run_local.py worker "$@"
