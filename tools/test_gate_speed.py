import subprocess
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import gate_image, gate_molecule, gate_selector, gate_timing


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    files = {
        "roles/accounts/meta/main.yml": "dependencies: []\n",
        "roles/ssh/meta/main.yml": "dependencies: [accounts]\n",
        "roles/docker/meta/main.yml": "dependencies: []\n",
        "roles/pool/meta/main.yml": "dependencies: []\n",
        "roles/accounts/molecule/default/converge.yml": (
            "- hosts: all\n  roles: [accounts]\n"
        ),
        "roles/ssh/molecule/default/converge.yml": "- hosts: all\n  roles: [ssh]\n",
        "roles/docker/molecule/default/converge.yml": (
            "- hosts: all\n  roles: [docker]\n"
        ),
        "roles/pool/molecule/default/converge.yml": (
            "- import_playbook: ../../../../playbooks/pool.yml\n"
        ),
        "playbooks/pool.yml": "- hosts: all\n  roles: [docker, pool]\n",
        "playbooks/ssh.yml": (
            "- hosts: all\n  tasks:\n    - include_role:\n        name: ssh\n"
        ),
    }
    for name, content in files.items():
        path = tmp_path / "infra/ansible" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (["infra/ansible/roles/pool/tasks/main.yml"], ["pool/default"]),
        (
            ["infra/ansible/roles/accounts/tasks/main.yml"],
            ["accounts/default", "ssh/default"],
        ),
        (
            ["infra/ansible/roles/docker/tasks/main.yml"],
            ["docker/default", "pool/default"],
        ),
        (["infra/ansible/playbooks/pool.yml"], ["docker/default", "pool/default"]),
        (["infra/ansible/playbooks/ssh.yml"], ["ssh/default"]),
        (["infra/ansible/inventory/hosts.yml"], "all"),
        (["infra/ansible/group_vars/all.yml"], "all"),
        (["tools/ops-ansible.sh"], "all"),
        (["infra/ansible/molecule/config.yml"], "all"),
        (["mise.toml"], "all"),
        (["tools/gate_selector.py"], "all"),
        (["README.md"], "all"),
        (["infra/ansible/roles/deleted/tasks/main.yml"], "all"),
        ([], []),
    ],
)
def test_selection(tree: Path, changes: list[str], expected: list[str] | str) -> None:
    if expected == "all":
        expected = ["accounts/default", "docker/default", "pool/default", "ssh/default"]
    assert gate_selector.select(tree, changes) == expected


@pytest.mark.parametrize("cached", [True, False])
def test_image_cache(tmp_path: Path, cached: bool) -> None:
    (tmp_path / "Dockerfile").write_text("FROM ubuntu@sha256:example\n")
    run = Mock()
    run.return_value.returncode = 0 if cached else 1
    image = gate_image.ensure(tmp_path, run=run)
    assert image.startswith("techops-molecule:")
    assert run.call_args_list[0].args[0] == ["docker", "image", "inspect", image]
    builds = [
        call for call in run.call_args_list if call.args[0][:2] == ["docker", "build"]
    ]
    assert bool(builds) is (not cached)
    assert all(call.kwargs.get("check") for call in builds)


def test_definition_changes_image(tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text("FROM first\n")
    before = gate_image.ensure(tmp_path, run=Mock())
    path.write_text("FROM second\n")
    assert gate_image.ensure(tmp_path, run=Mock()) != before


def test_phase_timings() -> None:
    lines = [
        "role\t-\tSTART\t100\n",
        "role\tdestroy\tPHASE\t100\n",
        "role\tcreate\tPHASE\t102\n",
        "role\tconverge\tPHASE\t105\n",
        "role\tidempotence\tPHASE\t111\n",
        "role\tverify\tPHASE\t115\n",
        "role\tdestroy\tPHASE\t124\n",
        "role\t-\tEND\t126\n",
        "partial\t-\tSTART\t126\n",
        "partial\tconverge\tPHASE\t127\n",
    ]
    assert gate_timing.summarize(lines) == {
        "role": {
            "total": 26,
            "destroy": 4,
            "create": 3,
            "converge": 6,
            "idempotence": 4,
            "verify": 9,
        },
        "partial": {"total": None},
    }


def test_failed_command_recorded(tmp_path: Path) -> None:
    record = tmp_path / "timings.tsv"
    assert gate_timing.command("lint", ["sh", "-c", "exit 7"], record) == 7
    assert "lint\t-\tFAILED\t" in record.read_text()


def test_local_and_ci_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    tasks = tomllib.loads((root / "mise.toml").read_text())["tasks"]
    assert "mise run test-ansible-selected" in tasks["test"]["run"]
    assert "mise run test-ansible" not in tasks["test"]["run"]
    assert tasks["check-commit"]["run"] == ["mise run lint", "mise run test"]
    assert "pytest tools" in str(tasks["test-tools"]["run"])
    assert "mise run test-ansible\n" in (root / ".github/workflows/ci.yml").read_text()


@pytest.mark.parametrize("selection", [["accounts/default"], []])
def test_runner_selection(
    tree: Path, monkeypatch: pytest.MonkeyPatch, selection: list[str]
) -> None:
    monkeypatch.chdir(tree)
    monkeypatch.setattr("sys.argv", ["gate_molecule"])
    monkeypatch.setattr(gate_molecule, "changed_paths", lambda *_: [])
    monkeypatch.setattr(gate_molecule, "select", lambda *_: selection)
    image = Mock(return_value="cached-image")
    run = Mock(return_value=0)
    monkeypatch.setattr(gate_molecule, "ensure", image)
    monkeypatch.setattr(gate_molecule, "command", run)
    gate_molecule.main()
    assert [call.args[0] for call in run.call_args_list] == selection
    assert [call.kwargs["target"] for call in image.call_args_list] == [
        name.split("/")[0] for name in selection
    ]


def test_unknown_playbook_role_selects_all(tree: Path) -> None:
    playbook = tree / "infra/ansible/playbooks/pool.yml"
    playbook.write_text("- hosts: all\n  roles: ['{{ dynamic_role }}']\n")
    assert gate_selector.select(tree, ["infra/ansible/playbooks/pool.yml"]) == [
        "accounts/default",
        "docker/default",
        "pool/default",
        "ssh/default",
    ]


def test_runner_unknown_base_selects_all(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tree)
    monkeypatch.setattr("sys.argv", ["gate_molecule"])
    monkeypatch.setattr(
        gate_molecule,
        "changed_paths",
        Mock(side_effect=subprocess.CalledProcessError(1, "git")),
    )
    monkeypatch.setattr(gate_molecule, "ensure", Mock(return_value="cached-image"))
    run = Mock(return_value=0)
    monkeypatch.setattr(gate_molecule, "command", run)
    gate_molecule.main()
    assert [call.args[0] for call in run.call_args_list] == [
        "accounts/default",
        "docker/default",
        "pool/default",
        "ssh/default",
    ]
