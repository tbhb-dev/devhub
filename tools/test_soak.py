import json
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import soak


def test_ac1_records_each_run_on_completion(tmp_path: Path) -> None:
    results = tmp_path / "soak.jsonl"
    observed_states: list[Path] = []
    flushed_counts: list[int] = []
    outputs = [
        (
            0,
            (
                "PLAY [Converge] ***\n\n"
                "TASK [ops_ssh : Install sshd drop-in] ***\n"
                "ok: [edge-1]\n"
            ),
        ),
        (
            1,
            (
                "\x1b[0;32mTASK [ops_ssh : Check sshd] ***\x1b[0m\n\n"
                'fatal: [edge-1]: FAILED! => {"msg": "ignored probe"}\n'
                "...ignoring\n"
                "TASK [docker_rootless : Read subordinate ID files] ***\n"
                'fatal: [edge-1]: UNREACHABLE! => {"msg": "Read timed out"}\n'
                "TASK [later : Never reached] ***\n"
                'fatal: [edge-1]: FAILED! => {"msg": "second"}\n'
            ),
        ),
    ]

    def fake_runner(scenario: str, root: Path, state: Path) -> tuple[int, str]:
        assert scenario == "actions_runner_pool/default"
        assert root == tmp_path
        assert state.is_dir()
        assert state.name.startswith("molecule-")
        lines = (
            results.read_text(encoding="utf-8").splitlines() if results.exists() else []
        )
        flushed_counts.append(len(lines))
        observed_states.append(state)
        return outputs[len(observed_states) - 1]

    loads = iter([(0.5, 0.4, 0.3), (1.2, 0.8, 0.5), (1.5, 1.0, 0.7), (2.0, 1.4, 0.9)])
    times = iter([100.0, 112.5, 200.0, 245.25])

    records = soak.Soak(
        scenario="actions_runner_pool/default",
        count=2,
        results=results,
        runner=fake_runner,
        root=tmp_path,
        load_averages=lambda: next(loads),
        clock=lambda: next(times),
        commit_fn=lambda _: "abc1234",
    ).run()

    assert flushed_counts == [0, 1]
    assert len(observed_states) == 2
    assert observed_states[0] != observed_states[1]
    assert not observed_states[0].exists()
    assert not observed_states[1].exists()

    on_disk = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    assert on_disk == records
    assert len(records) == 2
    assert records[0] == {
        "run": 1,
        "run_id": observed_states[0].name.removeprefix("molecule-"),
        "status": "pass",
        "wall_seconds": 12.5,
        "load_start": [0.5, 0.4, 0.3],
        "load_finish": [1.2, 0.8, 0.5],
        "commit": "abc1234",
        "failing_task": None,
        "error_line": None,
    }
    assert records[1] == {
        "run": 2,
        "run_id": observed_states[1].name.removeprefix("molecule-"),
        "status": "fail",
        "wall_seconds": 45.25,
        "load_start": [1.5, 1.0, 0.7],
        "load_finish": [2.0, 1.4, 0.9],
        "commit": "abc1234",
        "failing_task": "docker_rootless : Read subordinate ID files",
        "error_line": 'fatal: [edge-1]: UNREACHABLE! => {"msg": "Read timed out"}',
    }
    assert soak.parse_failure(
        "TASK [custom] ***\nok: [edge-1]\nsubprocess exited 2\n"
    ) == (
        "custom",
        "subprocess exited 2",
    )


def test_ac2_resume_continues_at_next_run_without_repeating(tmp_path: Path) -> None:
    results = tmp_path / "soak.jsonl"
    calls: list[int] = []

    def interrupted_runner(
        _scenario: str, _root: Path, _state: Path
    ) -> tuple[int, str]:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return 0, "TASK [setup] ***\nok: [edge-1]\n"
        if len(calls) == 2:
            return (
                1,
                'TASK [verify] ***\nfatal: [edge-1]: FAILED! => {"msg": "timeout"}\n',
            )
        raise RuntimeError

    with pytest.raises(RuntimeError):
        soak.Soak(
            scenario="actions_runner_pool/default",
            count=4,
            results=results,
            runner=interrupted_runner,
            root=tmp_path,
            load_averages=lambda: (1.0, 1.0, 1.0),
            clock=lambda: 10.0,
            commit_fn=lambda _: "deadbeef",
        ).run()

    partial = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["run"] for item in partial] == [1, 2]
    assert [item["status"] for item in partial] == ["pass", "fail"]

    # Ensure blank lines in the JSONL file are ignored on resume.
    with results.open("a", encoding="utf-8") as stream:
        stream.write("\n")

    resumed_calls: list[int] = []

    def resumed_runner(_scenario: str, _root: Path, _state: Path) -> tuple[int, str]:
        resumed_calls.append(len(resumed_calls) + 1)
        return 0, "TASK [verify] ***\nok: [edge-1]\n"

    final_records = soak.Soak(
        scenario="actions_runner_pool/default",
        count=4,
        results=results,
        runner=resumed_runner,
        root=tmp_path,
        load_averages=lambda: (2.0, 2.0, 2.0),
        clock=lambda: 20.0,
        commit_fn=lambda _: "deadbeef",
    ).run()

    assert len(resumed_calls) == 2
    assert [item["run"] for item in final_records] == [1, 2, 3, 4]
    assert [item["status"] for item in final_records] == [
        "pass",
        "fail",
        "pass",
        "pass",
    ]
    assert final_records[:2] == partial


def test_ac3_stops_after_consecutive_failures_without_retry(tmp_path: Path) -> None:
    results = tmp_path / "soak.jsonl"
    outcomes = iter(
        [
            (1, "TASK [t1] ***\nfatal: [edge-1]: FAILED! => {}\n"),
            (0, "TASK [t2] ***\nok: [edge-1]\n"),
            (1, "TASK [t3] ***\nfatal: [edge-1]: FAILED! => {}\n"),
            (1, "TASK [t4] ***\nfatal: [edge-1]: FAILED! => {}\n"),
            (1, "TASK [t5] ***\nfatal: [edge-1]: FAILED! => {}\n"),
            (0, "TASK [t6] ***\nok: [edge-1]\n"),
        ]
    )
    invocations = 0

    def fake_runner(_scenario: str, _root: Path, _state: Path) -> tuple[int, str]:
        nonlocal invocations
        invocations += 1
        return next(outcomes)

    records = soak.Soak(
        scenario="ops_ssh/default",
        count=10,
        results=results,
        max_failures=3,
        runner=fake_runner,
        root=tmp_path,
        load_averages=lambda: (0.1, 0.1, 0.1),
        clock=lambda: 1.0,
        commit_fn=lambda _: "11112222",
    ).run()

    assert invocations == 5
    assert [item["run"] for item in records] == [1, 2, 3, 4, 5]
    assert [item["status"] for item in records] == [
        "fail",
        "pass",
        "fail",
        "fail",
        "fail",
    ]


def test_ac4_cli_and_default_runner_without_agent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    monkeypatch.setenv("CODEX_THREAD_ID", "")

    root = Path(__file__).resolve().parents[1]
    assert len(soak.head_commit(root)) == 40

    results = tmp_path / "cli-soak.jsonl"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(soak, "head_commit", Mock(return_value="cafebabe"))

    with pytest.raises(SystemExit):
        soak.main([])

    exit_code = soak.main(
        ["ops_ssh/default", "--runs", "2", "--results", str(results)],
        runner=lambda _s, _r, _state: (0, "TASK [ok] ***\nok: [edge-1]\n"),
    )
    assert exit_code == 0
    lines = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    assert [item["run"] for item in lines] == [1, 2]

    state = tmp_path / "molecule-run42"
    state.mkdir()
    ensure_mock = Mock(return_value="techops-molecule:test")
    proc_mock = Mock(
        side_effect=[
            subprocess.CompletedProcess(
                args=["uv"], returncode=0, stdout="TASK [converge] ***\nok: [edge-1]\n"
            ),
            subprocess.CompletedProcess(args=["uv"], returncode=1, stdout=""),
        ]
    )
    code, output = soak.run_scenario(
        "ops_ssh/default",
        tmp_path,
        state,
        ensure_image=ensure_mock,
        run=proc_mock,
    )
    assert code == 0
    assert "TASK [converge]" in output
    code_empty, output_empty = soak.run_scenario(
        "ops_ssh",
        tmp_path,
        state,
        ensure_image=ensure_mock,
        run=proc_mock,
    )
    assert code_empty == 1
    assert output_empty == ""
    ensure_mock.assert_called_with(
        tmp_path / "infra/ansible/molecule/image", target="ops_ssh"
    )
    call = proc_mock.call_args_list[0]
    assert call.args[0] == [
        "uv",
        "run",
        "--group",
        "ops",
        "molecule",
        "test",
        "-s",
        "default",
    ]
    assert call.kwargs["cwd"] == tmp_path / "infra/ansible/roles/ops_ssh"
    assert call.kwargs["env"]["MOLECULE_RUN_ID"] == "run42"
    assert (
        call.kwargs["env"]["MOLECULE_EPHEMERAL_DIRECTORY"] == f"{state}/ops_ssh/default"
    )
    assert call.kwargs["env"]["MOLECULE_IMAGE"] == "techops-molecule:test"

    tasks = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))["tasks"]
    assert "soak" in tasks
    assert "python -m tools.soak" in str(tasks["soak"]["run"])


def test_risks_commit_hash_guard_refuses_and_start_over_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = tmp_path / "soak.jsonl"
    # Starting over when the results file does not yet exist succeeds cleanly.
    soak.Soak(
        scenario="ops_ssh/default",
        count=1,
        results=results,
        start_over=True,
        runner=lambda _s, _r, _state: (0, ""),
        root=tmp_path,
        load_averages=lambda: (0.2, 0.2, 0.2),
        clock=lambda: 5.0,
        commit_fn=lambda _: "commit-old",
    ).run()

    with pytest.raises(ValueError, match="commit"):
        soak.Soak(
            scenario="ops_ssh/default",
            count=2,
            results=results,
            runner=lambda _s, _r, _state: (0, ""),
            root=tmp_path,
            load_averages=lambda: (0.2, 0.2, 0.2),
            clock=lambda: 6.0,
            commit_fn=lambda _: "commit-new",
        ).run()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(soak, "head_commit", Mock(return_value="commit-new"))
    with pytest.raises(SystemExit, match="commit"):
        soak.main(
            ["ops_ssh/default", "2", "--results", str(results)],
            runner=lambda _s, _r, _state: (0, ""),
        )

    reset_records = soak.Soak(
        scenario="ops_ssh/default",
        count=2,
        results=results,
        start_over=True,
        runner=lambda _s, _r, _state: (0, ""),
        root=tmp_path,
        load_averages=lambda: (0.3, 0.3, 0.3),
        clock=lambda: 7.0,
        commit_fn=lambda _: "commit-new",
    ).run()

    assert [item["run"] for item in reset_records] == [1, 2]
    assert {item["commit"] for item in reset_records} == {"commit-new"}
    disk_records = [
        json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()
    ]
    assert disk_records == reset_records
