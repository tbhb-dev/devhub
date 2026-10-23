#!/usr/bin/env python3
import hashlib
import json
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SECRETS = Path("/etc/temporal-host")
HOME = Path("/var/lib/temporal")
EVIDENCE = Path(tempfile.gettempdir()) / "temporal-postgres-evidence.json"


def run(*args: str, stdin: str | None = None, check: bool = True) -> str:
    result = subprocess.run(
        args, input=stdin, text=True, capture_output=True, timeout=180, check=False
    )
    if check and result.returncode:
        message = f"{args}: {result.returncode}: {result.stderr}"
        raise AssertionError(message)
    return result.stdout.strip()


def sql(database: str, query: str, container: str = "temporal-postgres") -> str:
    return run(
        "docker",
        "exec",
        container,
        "psql",
        "-XAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        database,
        "-c",
        query,
    )


def checks(major: int) -> None:
    daemon = json.loads(run("docker", "info", "--format", "json"))
    assert "name=rootless" in daemon["SecurityOptions"]
    assert daemon["DockerRootDir"] == str(HOME / ".local/share/docker")
    assert sql("postgres", "SHOW server_version").startswith(f"{major}.")
    assert int(sql("postgres", "SHOW max_connections")) >= 100
    for database in ("temporal", "temporal_visibility"):
        assert (
            run(
                "docker",
                "exec",
                "-i",
                "temporal-postgres",
                "bash",
                "-c",
                "read -r PGPASSWORD; export PGPASSWORD; "
                "exec psql -XAt -v ON_ERROR_STOP=1 "
                '-h 127.0.0.1 -U temporal -d "$1" -c "SELECT current_user"',
                "bash",
                database,
                stdin=(SECRETS / "postgres-password").read_text() + "\n",
            )
            == "temporal"
        )
    info = json.loads(run("docker", "inspect", "temporal-postgres"))[0]
    image = info["Config"]["Image"]
    network = json.loads(run("docker", "network", "inspect", "temporal"))[0]
    assert network["Name"] == "temporal"
    address = info["NetworkSettings"]["Networks"]["temporal"]["IPAddress"]
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "temporal",
        image,
        "pg_isready",
        "-h",
        "temporal-postgres",
        "-t",
        "3",
    )
    run("docker", "network", "create", "temporal-other", check=False)
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "temporal-other",
            image,
            "pg_isready",
            "-h",
            address,
            "-t",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result
    assert not run("docker", "port", "temporal-postgres")
    listeners = run("ss", "-ltnH")
    assert all(
        line.split()[3].rsplit(":", 1)[-1] != "5432" for line in listeners.splitlines()
    )
    assert not info["HostConfig"]["PortBindings"]
    passwords = [
        (SECRETS / name).read_text()
        for name in ("postgres-password", "postgres-admin-password")
    ]
    assert passwords[0] != passwords[1]
    for password in passwords:
        assert re.fullmatch(r"[A-Za-z0-9]{1,48}", password)
        for container in run("docker", "ps", "-aq").splitlines():
            assert password not in run("docker", "inspect", container)
        for image_id in run("docker", "image", "ls", "-q").splitlines():
            assert password not in run("docker", "history", "--no-trunc", image_id)
        assert password not in run(
            "systemctl", "--user", "cat", "temporal-postgres.service"
        )
    assert (
        run("docker", "exec", "temporal-postgres", "ls", "-A", "/run/secrets")
        == "ready"
    )
    for name in ("temporal-postgres-dump",):
        assert not (HOME / ".local/bin" / name).exists()
    assert not (HOME / "dumps").exists()
    for unit in ("temporal-postgres-dump.timer", "temporal-postgres-dump.service"):
        assert not (HOME / ".config/systemd/user" / unit).exists()
    assert not Path("/var/lib/temporal-docker-data.img").exists()
    assert not list(Path("/etc/systemd/system").glob("*temporal*docker*.mount"))
    assert not (HOME / ".config/op").exists()
    assert not Path("/etc/temporal-postgres/op.env").exists()
    print(f"PostgreSQL {major}: authentication, isolation and credential checks passed")


def password_state() -> dict[str, list[str | int]]:
    return {
        p.name: [hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns]
        for p in SECRETS.glob("postgres*password")
    }


def side_effect() -> None:
    checks(16)
    EVIDENCE.write_text(json.dumps(password_state()))


def verify() -> None:
    checks(15)
    assert json.loads(EVIDENCE.read_text()) == password_state()
    print("Both passwords survived the second converge unchanged")


def home() -> None:
    passwords = [
        (SECRETS / name).read_bytes()
        for name in ("postgres-password", "postgres-admin-password")
    ]
    for name in ("postgres-password", "postgres-admin-password"):
        info = (SECRETS / name).stat()
        assert stat.S_IMODE(info.st_mode) == 0o400
        assert info.st_uid == HOME.stat().st_uid
        assert info.st_gid == HOME.stat().st_gid
    for path in [HOME, *HOME.rglob("*")]:
        info = path.lstat()
        assert info.st_uid != 0, path
        assert info.st_gid != 0, path
        if not stat.S_ISREG(info.st_mode):
            continue
        with path.open("rb") as stream:
            overlap = b""
            while block := stream.read(1024 * 1024):
                assert all(needle not in overlap + block for needle in passwords), path
                overlap = block[-48:]
    print(
        "No generated password in any regular file under the home; "
        "no root-owned entries"
    )


if __name__ == "__main__":
    {"side-effect": side_effect, "verify": verify, "home": home}[sys.argv[1]]()

# cspell:ignore isready
