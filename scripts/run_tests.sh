#!/bin/bash
# Runs the pytest suite (generator/tests, orchestrator/tests, trackcheck/tests) plus
# the trackcheck playlist lint against the real repo-root playlists.json.
#
# The shell's PYTHONPATH is set to /usr/lib/python3/dist-packages (so ad hoc
# scripts can reach system-installed packages), but that shadows this venv's
# own pytest/pluggy/packaging with older system versions and breaks pytest's
# own imports. Clearing it here is scoped to this script only.
cd "$(dirname "$0")/.."
env -u PYTHONPATH venv/bin/python3 -m pytest "$@"
PYTEST_STATUS=$?

# master_tracks_list.json is gitignored -- it's generated at runtime by
# gather_tracks.py from a live game install (see AGENTS.md), so it legitimately
# doesn't exist on a fresh checkout or in CI. Lint against it when present (fails
# loudly on real typos); skip with a warning rather than failing when it's absent --
# see trackcheck/tests for the fixture-backed tests that always run regardless.
if [ -f config/master_tracks_list.json ]; then
    echo "--- trackcheck: linting playlists.json against master_tracks_list.json ---"
    env -u PYTHONPATH venv/bin/python3 -m trackcheck.lint_playlists
    LINT_STATUS=$?
else
    echo "--- trackcheck: master_tracks_list.json not present -- skipping live playlist lint ---"
    LINT_STATUS=0
fi

if [ $PYTEST_STATUS -ne 0 ]; then
    exit $PYTEST_STATUS
fi
exit $LINT_STATUS
