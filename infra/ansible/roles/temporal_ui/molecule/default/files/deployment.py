#!/usr/bin/env python3
# cspell:ignore getpwnam
"""Temporal deployment acceptance checks, run as root inside Molecule."""

import hashlib
import json
import pwd
import socket
import subprocess
import sys
from pathlib import Path

STATE = Path("/opt/temporal-playbook-test")
PROBE_IMAGE = (
    "busybox@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e"
)


def run(*args: str) -> str:
    return subprocess.run(
        args, check=True, text=True, capture_output=True, timeout=180
    ).stdout


def user_run(user: str, *args: str) -> str:
    uid = pwd.getpwnam(user).pw_uid
    return run(
        "runuser",
        "-u",
        user,
        "--",
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"DOCKER_HOST=unix:///run/user/{uid}/docker.sock",
        *args,
    )


def listeners() -> list[str]:
    return sorted({line.split()[3] for line in run("ss", "-H", "-ltn").splitlines()})


def check_listeners(before: list[str], after: list[str], ports: list[int]) -> None:
    for endpoint in set(after) - set(before):
        assert endpoint.rsplit(":", 1)[0] == "127.0.0.1", endpoint
    for port in ports:
        matches = [value for value in after if value.rsplit(":", 1)[1] == str(port)]
        assert matches == [f"127.0.0.1:{port}"], matches


def digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def before_remove(args: list[str]) -> None:
    frontend, metrics, ui = map(int, args[:3])
    check_listeners(
        json.loads((STATE / "listeners.json").read_text()),
        listeners(),
        [frontend, metrics, ui],
    )
    # A refused probe is meaningful only while the actual frontend is reachable.
    with socket.create_connection(("127.0.0.1", frontend), timeout=5):
        pass
    frontend_service, ui_service = args[5:7]
    calls = (STATE / "tailscale.calls").read_text().splitlines()
    for service, port in [(frontend_service, frontend), (ui_service, ui)]:
        assert (
            f"serve --bg --service={service} --tcp={port} tcp://127.0.0.1:{port}"
            in calls
        ), calls
    assert not any(
        "--tls-terminated-tcp" in line or "--https" in line for line in calls
    ), calls
    # --network host means the second daemon's rootless network namespace.
    # Its default gateway is the host gateway, not this container's loopback.
    user_run("temporal-probe", "docker", "pull", PROBE_IMAGE)
    result = user_run(
        "temporal-probe",
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        PROBE_IMAGE,
        "sh",
        "-ec",
        "gateway=$(ip -4 route show default | awk '{print $3}'); "
        'test -n "$gateway"; '
        'if nc -w 3 "$gateway" 7233 </dev/null; then exit 1; '
        'else result=$?; test "$result" -eq 1; fi; echo refused',
    )
    assert result.strip() == "refused", result
    (STATE / "probe.result").write_text(result)
    data, dumps = map(Path, args[3:5])
    assert data.is_dir(), data
    assert any(data.iterdir()), data
    latest = max(dumps.glob("*.sql"), key=lambda path: path.stat().st_mtime_ns)
    assert latest.stat().st_size > 0, latest
    # PG_VERSION survives a clean shutdown; transient database files may not.
    retained = {str(path): digest(path) for path in data.rglob("PG_VERSION")}
    assert retained, data
    (STATE / "retained.json").write_text(
        json.dumps(
            {
                "data": str(data),
                "dump": str(latest),
                "dump_hash": digest(latest),
                "files": retained,
            }
        )
    )


def after_remove(args: list[str]) -> None:
    network, units, containers = args
    remaining = user_run(
        "temporal", "docker", "ps", "-a", "--format", "{{.Names}}"
    ).splitlines()
    assert not set(remaining).intersection(containers.split(",")), remaining
    networks = user_run(
        "temporal", "docker", "network", "ls", "--format", "{{.Name}}"
    ).splitlines()
    assert network not in networks, networks
    enabled = user_run(
        "temporal",
        "systemctl",
        "--user",
        "list-unit-files",
        "--state=enabled",
        "--no-legend",
        "--no-pager",
    ).split()
    assert not set(units.split(",")).intersection(enabled), enabled
    active = user_run(
        "temporal",
        "systemctl",
        "--user",
        "list-units",
        "--state=active",
        "--no-legend",
        "--no-pager",
    ).split()
    assert not set(units.split(",")).intersection(active), active
    for unit in ("temporal-frontend-tailnet.service", "temporal-ui-tailnet.service"):
        assert not Path("/etc/systemd/system", unit).exists(), unit
    assert (STATE / "probe.result").read_text().strip() == "refused"
    saved = json.loads((STATE / "retained.json").read_text())
    assert Path(saved["data"]).is_dir()
    assert digest(saved["dump"]) == saved["dump_hash"]
    assert all(digest(path) == checksum for path, checksum in saved["files"].items())


if __name__ == "__main__":
    command, *arguments = sys.argv[1:]
    if command == "baseline":
        (STATE / "listeners.json").write_text(json.dumps(listeners()))
    elif command == "before-remove":
        before_remove(arguments)
    elif command == "after-remove":
        after_remove(arguments)
    else:
        raise ValueError(command)
