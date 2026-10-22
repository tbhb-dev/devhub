#!/usr/bin/env python3
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STUB_VALUE = "molecule-stub-password-27"
HOME = Path("/var/lib/temporal")
DUMPS = HOME / "dumps"
EVIDENCE = Path(tempfile.gettempdir()) / "temporal-postgres-evidence.json"
RESTORE = "temporal-postgres-restore"


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
                stdin=STUB_VALUE + "\n",
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
    assert "temporal-postgres-dump.timer" in run(
        "systemctl", "--user", "list-timers", "--all", "--no-pager"
    )
    for container in run("docker", "ps", "-aq").splitlines():
        assert STUB_VALUE not in run("docker", "inspect", container)
    for image_id in run("docker", "image", "ls", "-q").splitlines():
        assert STUB_VALUE not in run("docker", "history", "--no-trunc", image_id)
    for unit in (
        "temporal-postgres.service",
        "temporal-postgres-dump.service",
        "temporal-postgres-dump.timer",
    ):
        assert STUB_VALUE not in run("systemctl", "--user", "cat", unit)
    print(f"AC1-5, AC6 timer, AC8 inspect/history/units: PostgreSQL {major} passed")


def side_effect() -> None:
    checks(16)
    for database in ("temporal", "temporal_visibility"):
        sql(
            database,
            "CREATE TABLE restore_probe (value integer); "
            "INSERT INTO restore_probe VALUES (27)",
        )
    for days in (15, 13):
        fixture = DUMPS / f"fixture-{days}.sql"
        fixture.write_text("-- aged dump fixture\n")
        modified = time.time() - days * 86400
        os.utime(fixture, (modified, modified))
    before = set(DUMPS.glob("temporal-*.sql"))
    # Trigger the real timer shortly, then remove only the test override.
    override = (
        Path(os.environ["XDG_RUNTIME_DIR"])
        / "systemd/user/temporal-postgres-dump.timer.d"
    )
    override.mkdir(parents=True)
    config = override / "molecule.conf"
    config.write_text("[Timer]\nOnCalendar=\nOnActiveSec=1s\n")
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "restart", "temporal-postgres-dump.timer")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        new = set(DUMPS.glob("temporal-*.sql")) - before
        if new and not (DUMPS / "fixture-15.sql").exists():
            break
        time.sleep(0.2)
    else:
        message = "the dump timer did not finish within 60 seconds"
        raise AssertionError(message)
    assert (DUMPS / "fixture-13.sql").exists()
    config.unlink()
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "restart", "temporal-postgres-dump.timer")
    dump = max(new)
    image = json.loads(run("docker", "inspect", "temporal-postgres"))[0]["Config"][
        "Image"
    ]
    run(
        "docker",
        "run",
        "-d",
        "--name",
        RESTORE,
        "--network",
        "temporal",
        "--env",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        image,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if run(
            "docker", "exec", RESTORE, "pg_isready", "-h", "127.0.0.1", check=False
        ).endswith("accepting connections"):
            break
        time.sleep(0.2)
    else:
        message = "the fresh restore container did not start"
        raise AssertionError(message)
    sql("postgres", "CREATE ROLE temporal LOGIN", RESTORE)
    run(
        "docker",
        "exec",
        "-i",
        RESTORE,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "postgres",
        stdin=dump.read_text(),
    )
    EVIDENCE.write_text(json.dumps({"version": 16, "dump": dump.name, "timer": True}))
    print(
        "AC6-7: timer dumped both databases, pruned the expired fixture "
        "and restored the dump"
    )


def verify() -> None:
    checks(15)
    evidence = json.loads(EVIDENCE.read_text())
    assert evidence["version"] == 16
    assert evidence["timer"]
    assert (DUMPS / evidence["dump"]).is_file()
    assert not (DUMPS / "fixture-15.sql").exists()
    assert (DUMPS / "fixture-13.sql").exists()
    for database in ("temporal", "temporal_visibility"):
        assert sql(database, "SELECT value FROM restore_probe", RESTORE) == "27"
    print("AC1, AC6-7: both versions and both restored databases passed")


def home() -> None:
    needle = STUB_VALUE.encode()
    for path in [HOME, *HOME.rglob("*")]:
        info = path.lstat()
        assert info.st_uid != 0, path
        assert info.st_gid != 0, path
        if not stat.S_ISREG(info.st_mode):
            continue
        with path.open("rb") as stream:
            overlap = b""
            while block := stream.read(1024 * 1024):
                assert needle not in overlap + block, path
                overlap = block[-len(needle) :]
    print(
        "AC8-9: no stub password in any regular file under the home; "
        "no root-owned entries"
    )


if __name__ == "__main__":
    {"side-effect": side_effect, "verify": verify, "home": home}[sys.argv[1]]()

# cspell:ignore isready
