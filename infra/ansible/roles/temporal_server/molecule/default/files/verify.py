#!/usr/bin/python3
# cspell:ignore cafile getuid noout enddate
"""Acceptance probes run inside the isolated Molecule host as temporal."""

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, text=True, capture_output=True, check=check, timeout=120
    )


home = Path.home()
config = home / ".config/temporal-server"
settings = json.loads((config / "namespaces.json").read_text())
tls = config / "tls"
base = [
    "docker",
    "run",
    "--rm",
    "--network",
    "container:temporal-server",
    "--user",
    "0",
    "--mount",
    f"type=bind,src={tls},dst=/tls,readonly",
    "--entrypoint",
    "temporal",
    settings["admin_image"],
    "--address",
    "127.0.0.1:7233",
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

# AC1: real health RPC, one server process and explicit four services.
run(*base, "operator", "cluster", "health")
info = json.loads(run("docker", "inspect", "temporal-server").stdout)[0]
assert info["Config"]["Image"].startswith("temporalio/server:1.32.0@sha256:")
processes = run(
    "docker", "top", "temporal-server", "-eo", "pid,args"
).stdout.splitlines()[1:]
assert len(processes) == 1, processes
for service in ("frontend", "history", "matching", "worker"):
    assert f"--service {service}" in processes[0], processes

# AC3/4: query both namespace configs and all attribute types without mutation.
for namespace in settings["namespaces"]:
    flags = ["--namespace", namespace["name"]]
    data = json.loads(run(*base, "operator", "namespace", "describe", *flags).stdout)
    ns_config = data["config"]
    assert (
        ns_config["workflowExecutionRetentionTtl"]
        == f"{namespace['retention_days'] * 86400}s"
    )
    for kind in ("history", "visibility"):
        assert ns_config[f"{kind}ArchivalState"] == "ARCHIVAL_STATE_DISABLED"
    attrs = json.loads(
        run(*base, "operator", "search-attribute", "list", *flags).stdout
    )["customAttributes"]
    for attr in settings["attributes"]:
        assert attrs[attr["name"]] == "INDEXED_VALUE_TYPE_" + attr["type"].upper()

# AC5: positive and two negative frontend handshakes on host loopback.
for certificate in ("client", None, "wrong"):
    context = ssl.create_default_context(cafile=str(tls / "ca.pem"))
    if certificate == "client":
        context.load_cert_chain(str(tls / "client.pem"), str(tls / "client.key"))
    elif certificate == "wrong":
        context.load_cert_chain(
            "/run/temporal-fixtures/wrong.pem", "/run/temporal-fixtures/wrong.key"
        )
    # TLS 1.2 completes client authentication during the handshake.
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        with (
            socket.create_connection(("127.0.0.1", 7233), timeout=5) as connection,
            context.wrap_socket(connection, server_hostname=settings["server_name"]),
        ):
            assert certificate == "client", "Unauthenticated client accepted"
    except ssl.SSLError:
        assert certificate != "client", "Valid certificate rejected"

# AC6: the history listener is internal; probe within the server's network namespace.
admin = (
    "temporalio/admin-tools:1.32.0@sha256:"
    "a9f84fb9a374b2374fe2e67c8efc0468ff3f1c66c8a0b14597ec86e349e62bca"
)
probe_code = (
    "import socket,ssl; c=ssl.create_default_context(cafile='/tls/ca.pem'); "
    "c.maximum_version=ssl.TLSVersion.TLSv1_2; "
    "c.wrap_socket(socket.create_connection(('127.0.0.1',7234),timeout=5),"
    "server_hostname='temporal.example.com')"
)
probe = run(
    "docker",
    "run",
    "--rm",
    "--network",
    "container:temporal-server",
    "--user",
    "0",
    "--mount",
    f"type=bind,src={tls},dst=/tls,readonly",
    "--entrypoint",
    "python3",
    "temporal-server-probe",
    "-c",
    probe_code,
    check=False,
)
assert probe.returncode != 0, probe.stderr
assert re.search(
    "handshake.failure|certificate.required", probe.stderr, re.IGNORECASE
), probe.stderr

# The negative frontend cases also exercise the CLI's namespace RPC.
for certificate in (None, "wrong"):
    args = [
        "docker",
        "run",
        "--rm",
        "--network",
        "container:temporal-server",
        "--user",
        "0",
        "--mount",
        "type=bind,src=/run/temporal-fixtures,dst=/fixtures,readonly",
        "--entrypoint",
        "temporal",
        admin,
        "--address",
        "127.0.0.1:7233",
        "--tls-ca-path",
        "/fixtures/ca.pem",
        "--tls-server-name",
        "temporal.example.com",
        "--command-timeout",
        "10s",
    ]
    if certificate:
        args += [
            "--tls-cert-path",
            "/fixtures/wrong.pem",
            "--tls-key-path",
            "/fixtures/wrong.key",
        ]
    refused = run(
        *args, "operator", "namespace", "describe", "--namespace", "coord", check=False
    )
    assert refused.returncode != 0
    assert re.search("TLS|certificate|handshake", refused.stderr, re.IGNORECASE), (
        refused.stderr
    )

# AC7: read what is mounted, rather than only the source file.
dynamic = run("docker", "exec", "temporal-server", "cat", "/dynamic.yml").stdout
assert not re.search(
    r"limit\.history(?:Size|Count)|(?:matching|frontend)\.[^:]*?(?:[Rr]ate|[Rr][Pp][Ss])",
    dynamic,
)

# AC8: ss and inspect must agree on loopback.
# Exercise both loopback and external probes.
listeners = run("ss", "-H", "-ltn").stdout.splitlines()
for port in (7233, 8000):
    matching = [
        line.split()[3] for line in listeners if line.split()[3].endswith(f":{port}")
    ]
    assert matching == [f"127.0.0.1:{port}"], matching
    assert info["HostConfig"]["PortBindings"][
        f"{7233 if port == 7233 else 8000}/tcp"
    ] == [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
    interfaces = json.loads(
        run("ip", "-j", "-4", "address", "show", "scope", "global").stdout
    )
    addresses = [
        address["local"]
        for interface in interfaces
        for address in interface["addr_info"]
    ]
    assert addresses
    for address in addresses:
        try:
            with socket.create_connection((address, port), timeout=2):
                message = "Port exposed on a non-loopback address"
                raise AssertionError(message)
        except OSError:
            pass
with urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=10) as response:
    assert re.search(rb"(?:temporal|service_requests|history_)\w*", response.read())

# AC9: validate every metric against openssl, including the CA.
textfile = Path("/var/lib/temporal-textfile")
assert textfile.stat().st_uid == os.getuid()
assert textfile.stat().st_mode & 0o002 == 0
metrics = (textfile / "temporal-server.prom").read_text()
for name in ("ca", "server", "client"):
    expiry = run(
        "openssl", "x509", "-in", str(tls / f"{name}.pem"), "-noout", "-enddate"
    ).stdout.strip()
    timestamp = int(ssl.cert_time_to_seconds(expiry.removeprefix("notAfter=")))
    assert (
        f'temporal_cert_expiry_timestamp_seconds{{certificate="{name}"}} {timestamp}'
        in metrics
    )

# AC2: no schema auto-setup in the server image; a fresh database must be refused.
run(
    "docker",
    "exec",
    "temporal-postgres",
    "psql",
    "-U",
    "postgres",
    "-d",
    "temporal",
    "-c",
    "CREATE DATABASE temporal_unmigrated",
)
runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "temporal-server"
stub = runtime / "stub.yaml"
source = (runtime / "docker.yaml").read_text()
stub.write_text(
    source.replace(
        "databaseName: temporal_visibility", "databaseName: temporal_unmigrated"
    ).replace("databaseName: temporal\n", "databaseName: temporal_unmigrated\n")
)
try:
    failed = run(
        "docker",
        "run",
        "--rm",
        "--user",
        "0",
        "--network",
        "temporal",
        "--mount",
        f"type=bind,src={runtime},dst=/runtime,readonly",
        "--mount",
        f"type=bind,src={tls},dst=/tls,readonly",
        "--mount",
        f"type=bind,src={config / 'dynamic.yml'},dst=/dynamic.yml,readonly",
        "--entrypoint",
        "temporal-server",
        info["Config"]["Image"],
        "--root",
        "/",
        "--config",
        "runtime",
        "--env",
        "stub",
        "start",
        "--service",
        "frontend",
        check=False,
    )
    assert failed.returncode != 0
    assert re.search(
        "schema.*version|version.*schema", failed.stdout + failed.stderr, re.IGNORECASE
    )
finally:
    stub.unlink()
    run(
        "docker",
        "exec",
        "temporal-postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "temporal",
        "-c",
        "DROP DATABASE temporal_unmigrated",
    )
sys.stdout.write("AC1-AC9 acceptance probes passed\n")
