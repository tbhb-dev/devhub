#!/usr/bin/env python3
# cspell:ignore getpwnam
"""Temporal deployment acceptance checks, run as root inside Molecule."""

import json
import pwd
import socket
import stat
import subprocess
import sys
from pathlib import Path

STATE = Path("/opt/temporal-playbook-test")
PROBE_IMAGE = (
    "busybox@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e"
)


def without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


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


def deployment(args: list[str]) -> None:
    check_listeners(
        json.loads((STATE / "listeners.json").read_text()), listeners(), [7233, 8080]
    )
    assert not any(endpoint.endswith(":8000") for endpoint in listeners())
    with socket.create_connection(("127.0.0.1", 7233), timeout=5):
        pass
    frontend_service, ui_service = args
    calls = (STATE / "tailscale.calls").read_text().splitlines()
    frontend_call = (
        f"serve --bg --service={frontend_service} --tcp=7233 tcp://127.0.0.1:7233"
    )
    ui_call = f"serve --bg --service={ui_service} --https=443 http://127.0.0.1:8080"
    assert frontend_call in calls, calls
    assert ui_call in calls, calls
    assert calls.index(frontend_call) < calls.index(ui_call), calls
    assert not any("--tls-terminated-tcp" in line for line in calls), calls
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


def files(args: list[str]) -> None:
    home = Path("/var/lib/temporal")
    uid = pwd.getpwnam("temporal").pw_uid
    gid = pwd.getpwnam("temporal").pw_gid
    specification = json.loads((STATE / "spec.json").read_text())
    expected = {
        "docker.service": (".config/systemd/user/docker.service", 0o644),
        "docker-drop-in.conf": (
            ".config/systemd/user/docker.service.d/temporal-server.conf",
            0o600,
        ),
        "postgres-init": (".config/temporal-postgres/init.sh", 0o644),
        "postgres-start": (".local/bin/temporal-postgres-start", 0o700),
        "postgres.service": (".config/systemd/user/temporal-postgres.service", 0o644),
        "config.yml": (".config/temporal-server/config.yml", 0o600),
        "dynamic.yml": (".config/temporal-server/dynamic.yml", 0o600),
        "server-start": (".local/bin/temporal-server-start", 0o700),
        "server.service": (".config/systemd/user/temporal-server.service", 0o600),
        "ui.env": (".config/temporal-ui/ui.env", 0o600),
        "ui.service": (".config/systemd/user/temporal-ui.service", 0o644),
    }
    for fixture, (relative, mode) in expected.items():
        path = home / relative
        content = specification[fixture].replace("/run/user/1000", f"/run/user/{uid}")
        actual = path.read_text()
        if fixture == "docker.service":
            # The shared docker_rootless unit carries explanatory comments that
            # the Temporal specification does not.
            actual = without_comments(actual)
            content = without_comments(content)
        assert actual == content, path
        info = path.stat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            uid,
            gid,
            mode,
        ), path
    for kind, service in zip(("frontend", "ui"), args, strict=True):
        path = Path(f"/etc/systemd/system/temporal-{kind}-tailnet.service")
        content = specification[f"{kind}-tailnet.service"].replace(
            f"svc:temporal-{kind}", service
        )
        assert path.read_text() == content, path
        info = path.stat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            0,
            0,
            0o644,
        ), path
    for root, names, mode in (
        (
            Path("/etc/temporal-host"),
            [
                "ca.pem",
                "server.pem",
                "server.key",
                "client.pem",
                "client.key",
                "postgres-password",
                "postgres-admin-password",
            ],
            0o400,
        ),
        (
            home / ".config/temporal-ui/tls",
            ["ca.pem", "certificate.pem", "private-key.pem"],
            0o400,
        ),
        (
            home / ".config/temporal-server/tls",
            ["ca.pem", "server.pem", "server.key", "client.pem", "client.key"],
            0o600,
        ),
    ):
        for name in names:
            path = root / name
            info = path.stat()
            assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
                uid,
                gid,
                mode,
            ), path
    for name in ("ca.pem", "server.pem", "server.key", "client.pem", "client.key"):
        assert (home / ".config/temporal-server/tls" / name).read_bytes() == (
            Path("/etc/temporal-host") / name
        ).read_bytes()
    for relative in (
        "dumps",
        ".config/op",
        ".local/libexec/temporal-ui-certificates",
        ".local/bin/temporal-postgres-dump",
        ".local/bin/temporal-server-reconcile",
        ".config/temporal-server/namespaces.json",
        ".config/systemd/user/temporal-postgres-dump.service",
        ".config/systemd/user/temporal-postgres-dump.timer",
    ):
        assert not (home / relative).exists(), relative
    assert not Path("/var/lib/temporal-docker-data.img").exists()
    assert not Path("/var/lib/temporal-textfile").exists()
    assert not Path("/etc/temporal-postgres/op.env").exists()


if __name__ == "__main__":
    command, *arguments = sys.argv[1:]
    if command == "baseline":
        (STATE / "listeners.json").write_text(json.dumps(listeners()))
    elif command == "deployment":
        deployment(arguments)
    elif command == "files":
        files(arguments)
    else:
        raise ValueError(command)
