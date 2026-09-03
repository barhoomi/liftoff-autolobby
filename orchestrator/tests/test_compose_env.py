"""docker-compose.yml carries FPV_WORKSHOP_CONTENT_DIR on BOTH services.

Asserted by a test rather than by eye (workshop-ingest-hardening.md §3.2 / criterion 5):
the bot and the dashboard both resolve workshop item directories, and the failure mode
of them disagreeing is silent -- the bot scans 0 workshop tracks and every freshly
downloaded track simply never appears, which is exactly what happened live on
2026-09-03 and took a session to diagnose.

Read as TEXT with a hand-rolled service splitter, not through a YAML parser: the repo
has no YAML dependency and this test is not worth adding one for -- the same call
orchestrator/tests/test_plugin_split_audit.py already makes for C#. The file's own
formatting (services at two-space indent, keys under them at four) is the contract.
"""

import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
COMPOSE_PATH = os.path.join(REPO_ROOT, "docker-compose.yml")

SERVICE_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


def service_blocks():
    """{service name: its block's text}, from the `services:` mapping only."""
    with open(COMPOSE_PATH, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    try:
        start = lines.index("services:") + 1
    except ValueError:  # pragma: no cover - the file always has one
        raise AssertionError("docker-compose.yml has no top-level `services:` key")

    blocks = {}
    current = None
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break  # a new top-level key (volumes:) ends the services mapping
        m = SERVICE_RE.match(line)
        if m:
            current = m.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


def env_value(block, key):
    m = re.search(r"^\s*{}:\s*(.+?)\s*$".format(re.escape(key)), block, re.M)
    return m.group(1) if m else None


class TestWorkshopContentDirIsSetForBothServices:
    def test_both_services_exist(self):
        blocks = service_blocks()
        assert "bot" in blocks and "dashboard" in blocks, sorted(blocks)

    def test_bot_and_dashboard_both_declare_it(self):
        blocks = service_blocks()
        for name in ("bot", "dashboard"):
            assert env_value(blocks[name], "FPV_WORKSHOP_CONTENT_DIR"), (
                "service '{}' does not set FPV_WORKSHOP_CONTENT_DIR -- it will resolve "
                "workshop items under /home/<user>/... and find nothing".format(name))

    def test_the_two_values_are_identical(self):
        blocks = service_blocks()
        bot = env_value(blocks["bot"], "FPV_WORKSHOP_CONTENT_DIR")
        dashboard = env_value(blocks["dashboard"], "FPV_WORKSHOP_CONTENT_DIR")
        assert bot == dashboard, (bot, dashboard)

    def test_it_points_at_the_410340_content_root(self):
        value = env_value(service_blocks()["bot"], "FPV_WORKSHOP_CONTENT_DIR")
        assert value.endswith("/steamapps/workshop/content/410340"), value


class TestDocumentedExecConvention:
    def test_the_runbook_comment_names_user_botuser(self):
        """§8.4: `docker compose exec` defaults to root, and a root-invoked CLI run left
        root-owned protocol files the plugin could not write."""
        with open(COMPOSE_PATH, encoding="utf-8") as fh:
            text = fh.read()
        assert "exec --user botuser bot python3 orchestrator/download_workshop_item.py" in text
