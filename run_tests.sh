#!/bin/bash
# Runs the pytest suite (generator/tests, orchestrator/tests).
#
# The shell's PYTHONPATH is set to /usr/lib/python3/dist-packages (so ad hoc
# scripts can reach system-installed packages), but that shadows this venv's
# own pytest/pluggy/packaging with older system versions and breaks pytest's
# own imports. Clearing it here is scoped to this script only.
cd "$(dirname "$0")"
env -u PYTHONPATH venv/bin/python3 -m pytest "$@"
