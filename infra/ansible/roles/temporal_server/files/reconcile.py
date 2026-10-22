#!/usr/bin/python3
"""Reconcile only missing namespaces, changed retention, and missing attributes."""

import json
import subprocess
import sys
import time
from pathlib import Path

with Path(sys.argv[1]).open(encoding="utf-8") as stream:
    settings = json.load(stream)
base = [
    "docker",
    "run",
    "--rm",
    "--network",
    "container:temporal-server",
    "--user",
    "0",
    "--mount",
    f"type=bind,src={Path.home() / '.config/temporal-server/tls'},dst=/tls,readonly",
    "--entrypoint",
    "temporal",
    settings["admin_image"],
    "--address",
    "127.0.0.1:7233",
    "--command-timeout",
    "5s",
    "--tls-ca-path",
    "/tls/ca.pem",
    "--tls-cert-path",
    "/tls/client.pem",
    "--tls-key-path",
    "/tls/client.key",
    "--tls-server-name",
    settings["server_name"],
    "--output",
    "json",
]


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    # S603: argv is a fixed list from the role's own settings, never a shell string.
    return subprocess.run(  # noqa: S603
        [*base, *args], text=True, capture_output=True, check=False
    )


def checked(*args: str) -> str:
    result = cli(*args)
    if result.returncode:
        sys.exit(result.stderr)
    return result.stdout


for _ in range(60):
    health = cli("operator", "cluster", "health")
    if health.returncode == 0:
        break
    time.sleep(2)
else:
    sys.exit(health.stderr)

namespaces = json.loads(checked("operator", "namespace", "list"))
existing = {ns["namespaceInfo"]["name"]: ns for ns in namespaces}
for namespace in settings["namespaces"]:
    name = namespace["name"]
    desired = namespace["retention_days"] * 86400
    current = existing.get(name)
    flags = [
        "--namespace",
        name,
        "--retention",
        f"{desired}s",
        "--history-archival-state",
        "disabled",
        "--visibility-archival-state",
        "disabled",
    ]
    if current is None:
        checked("operator", "namespace", "create", *flags)
        sys.stdout.write("changed\n")
    else:
        config = current["config"]
        if (
            config["workflowExecutionRetentionTtl"] != f"{desired}s"
            or config["historyArchivalState"] != "ARCHIVAL_STATE_DISABLED"
            or config["visibilityArchivalState"] != "ARCHIVAL_STATE_DISABLED"
        ):
            checked("operator", "namespace", "update", *flags)
            sys.stdout.write("changed\n")
    # A freshly registered namespace takes a cache refresh to become visible.
    for _ in range(60):
        result = cli("operator", "search-attribute", "list", "--namespace", name)
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        sys.exit(result.stderr)
    attributes = json.loads(result.stdout).get("customAttributes", {})
    for attribute in settings["attributes"]:
        attr_name, attr_type = attribute["name"], attribute["type"]
        if attr_name in attributes:
            if attributes[attr_name] != "INDEXED_VALUE_TYPE_" + attr_type.upper():
                sys.exit(f"Search attribute {attr_name} has a different type")
        else:
            checked(
                "operator",
                "search-attribute",
                "create",
                "--namespace",
                name,
                "--name",
                attr_name,
                "--type",
                attr_type,
            )
            sys.stdout.write("changed\n")
