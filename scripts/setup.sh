#!/usr/bin/env bash
# Prepare the environment so tests and the services can run.
# Used by the Claude Code SessionStart hook and handy for fresh checkouts.
set -euo pipefail

cd "$(dirname "$0")/.."

PIP="python3 -m pip install --quiet --ignore-installed"

# Install runtime + dev dependencies (idempotent).
$PIP -r requirements-dev.txt

echo "setup complete: $(python3 -c 'import flask, sqlalchemy; print("flask", flask.__version__, "sqlalchemy", sqlalchemy.__version__)')"
