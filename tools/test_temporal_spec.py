"""The checked-in fixtures transcribe scripts 02, 03, 05, 06 and 07."""

import json
from pathlib import Path

import pytest
import yaml
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "infra/ansible"
ROLES = ANSIBLE / "roles"
SPEC = json.loads((ROLES / "temporal_ui/molecule/default/files/spec.json").read_text())


def variables() -> dict[str, object]:
    values = {"docker_rootless_uid": 1000, "ansible_managed": "Ansible managed"}
    for role in ("temporal_postgres", "temporal_server", "temporal_ui"):
        values.update(yaml.safe_load((ROLES / role / "defaults/main.yml").read_text()))
    values.update(yaml.safe_load((ROLES / "temporal_server/vars/main.yml").read_text()))
    return {
        k: trust_as_template(v) if isinstance(v, str) else v for k, v in values.items()
    }


@pytest.mark.parametrize(
    ("role", "template", "fixture"),
    [
        ("temporal_postgres", "start.sh.j2", "postgres-start"),
        ("temporal_postgres", "temporal-postgres.service.j2", "postgres.service"),
        ("temporal_server", "config.yml.j2", "config.yml"),
        ("temporal_server", "temporal-server-start.sh.j2", "server-start"),
        ("temporal_server", "temporal-server.service.j2", "server.service"),
        ("temporal_ui", "ui.env.j2", "ui.env"),
        ("temporal_ui", "temporal-ui.service.j2", "ui.service"),
    ],
)
def test_rendered_spec(role: str, template: str, fixture: str) -> None:
    rendered = Templar(variables=variables()).template(
        trust_as_template((ROLES / role / "templates" / template).read_text())
    )
    assert rendered == SPEC[fixture]


def without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def test_docker_and_initialization_spec() -> None:
    unit = (ROLES / "docker_rootless/files/docker.service").read_text()
    assert without_comments(unit) == without_comments(SPEC["docker.service"])
    assert (ROLES / "temporal_postgres/files/init.sh").read_text() == SPEC[
        "postgres-init"
    ]


def test_local_password_contract() -> None:
    tasks = (ROLES / "temporal_postgres/tasks/main.yml").read_text()
    assert "postgres-admin-password" in tasks
    assert "postgres-password" in tasks
    assert "no_log: true" in tasks
    assert "creates:" in tasks
    assert "head -c 36 /dev/urandom | base64" in tasks
    assert 'mode: "0400"' in tasks


def test_certificate_prerequisites() -> None:
    for role in ("temporal_server", "temporal_ui"):
        tasks = (ROLES / role / "tasks/main.yml").read_text()
        assert "ansible.builtin.stat:" in tasks
        assert "provisioned out of band" in tasks
        assert "'0400'" in tasks
        assert "'temporal'" in tasks


def test_no_data_image() -> None:
    for file in (
        ANSIBLE / "playbooks/temporal.yml",
        ROLES / "temporal_postgres/tasks/main.yml",
    ):
        assert 'docker_rootless_data_size: ""' in file.read_text()


def test_no_obsolete_resources_or_delegation() -> None:
    files = list((ANSIBLE / "playbooks").rglob("*"))
    for role in ("temporal_postgres", "temporal_server", "temporal_ui"):
        files.extend((ROLES / role).rglob("*"))
    for file in files:
        if not file.is_file() or "molecule" in file.parts or file.suffix == ".md":
            continue
        text = file.read_text()
        assert "delegate_to:" not in text, file
        assert "temporal_textfile_dir" not in text, file
        assert "temporal_server_metrics_port" not in text, file
        assert "temporal_postgres_dump_" not in text, file
        assert "_op_" not in text, file
    for file in (
        ANSIBLE / "playbooks/temporal-remove.yml",
        ANSIBLE / "playbooks/files/temporal-host-read-secret.sh",
        ROLES / "temporal_server/files/reconcile.py",
        ROLES / "temporal_ui/files/temporal-ui-certificates.sh",
    ):
        assert not file.exists(), file


@pytest.mark.parametrize(
    ("kind", "fixture"),
    [("frontend", "frontend-tailnet.service"), ("ui", "ui-tailnet.service")],
)
def test_tailnet_spec(kind: str, fixture: str) -> None:
    values = {
        "temporal_frontend_service": "svc:temporal-frontend",
        "temporal_ui_service": "svc:temporal-ui",
        "temporal_server_frontend_port": 7233,
        "temporal_ui_loopback_port": 8080,
        "ansible_managed": "Ansible managed",
    }
    rendered = Templar(variables=values).template(
        trust_as_template(
            (
                ANSIBLE / f"playbooks/templates/temporal-{kind}-tailnet.service.j2"
            ).read_text()
        )
    )
    assert rendered == SPEC[fixture]


def test_tailnet_start_order() -> None:
    tasks = yaml.safe_load(
        (ANSIBLE / "playbooks/tasks/temporal-services.yml").read_text()
    )
    starts = [
        t["ansible.builtin.systemd_service"]
        for t in tasks
        if "ansible.builtin.systemd_service" in t
    ]
    assert [t["name"] for t in starts] == [
        "temporal-frontend-tailnet.service",
        "temporal-ui-tailnet.service",
    ]
    assert all("loop" not in t for t in tasks if "ansible.builtin.systemd_service" in t)


def test_stub_needs_no_password_manager() -> None:
    stub = (ANSIBLE / "tests/inventory/group_vars/all.yml").read_text()
    assert "temporal_postgres_op_" not in stub
