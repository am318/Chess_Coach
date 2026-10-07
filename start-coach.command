#!/bin/bash
# Double-click in Finder (or run ./start-coach.command) to launch the Chess Coach.
# Creates the venv on first run, then starts the web app and opens a browser tab.
set -e
cd "$(dirname "$0")"

export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating environment (needs uv + stockfish from Homebrew)…"
  command -v uv >/dev/null || { echo "Install uv:  brew install uv stockfish"; exit 1; }
  uv venv --python 3.12
  uv pip install -e ".[dev,coach]"
fi

if [ ! -f coach.toml ]; then
  cp coach.example.toml coach.toml
  echo "Created coach.toml — edit it to add your Lichess / Chess.com usernames, then re-run."
  open -e coach.toml || true
  exit 0
fi

exec .venv/bin/python -m coach.cli serve --open
