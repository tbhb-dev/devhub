import json
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import relay_diag


def test_ac1_records_interval_timings_and_host_state(tmp_path: Path) -> None:
    output = tmp_path / "diag" / "relay.jsonl"
    observed_calls: list[tuple[str, ...]] = []

    def fake_client(argv: relay_diag.Sequence[str]) -> int:
        observed_calls.append(tuple(argv))
        return 0

    times = iter(
        [
            10.0,
            10.05,
            10.05,
            10.42,
            10.42,
            10.18 + 0.42,
            40.60,
            40.64,
            40.64,
            40.74,
            40.74,
            41.29,
        ]
    )
    loads = iter([(0.5, 0.4, 0.3), (1.5, 1.2, 0.9)])
    relays = iter([2, 3])
    timeouts = iter([10, 60])
    stamps = iter(["2026-10-24T01:00:00+00:00", "2026-10-24T01:00:30+00:00"])
    sleeps: list[float] = []

    records = relay_diag.RelayDiag(
        output=output,
        minutes=1.0,
        interval=30.0,
        threshold=5.0,
        client=fake_client,
        relay_count_fn=lambda: next(relays),
        load_averages=lambda: next(loads),
        ansible_timeout_fn=lambda: next(timeouts),
        clock=lambda: next(times),
        sleep=sleeps.append,
        timestamp_fn=lambda: next(stamps),
    ).run()

    assert len(records) == 2
    assert sleeps == [30.0]
    assert records[0] == {
        "timestamp": "2026-10-24T01:00:00+00:00",
        "timings": {
            "docker version": 0.05,
            "docker ps": 0.37,
            "docker info": 0.18,
        },
        "slowest_call": "docker ps",
        "slowest_duration": 0.37,
        "relay_process_count": 2,
        "load_averages": [0.5, 0.4, 0.3],
        "ansible_default_timeout": 10,
    }
    assert records[1] == {
        "timestamp": "2026-10-24T01:00:30+00:00",
        "timings": {
            "docker version": 0.04,
            "docker ps": 0.1,
            "docker info": 0.55,
        },
        "slowest_call": "docker info",
        "slowest_duration": 0.55,
        "relay_process_count": 3,
        "load_averages": [1.5, 1.2, 0.9],
        "ansible_default_timeout": 60,
    }
    on_disk = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert on_disk == records


def test_ac2_writes_slow_call_line_above_threshold(tmp_path: Path) -> None:
    output = tmp_path / "relay.jsonl"
    times = iter([100.0, 100.2, 100.2, 101.7, 101.7, 111.735])
    stamps = iter(
        [
            "2026-10-24T02:00:01+00:00",
            "2026-10-24T02:00:11+00:00",
            "2026-10-24T02:00:12+00:00",
        ]
    )

    records = relay_diag.RelayDiag(
        output=output,
        minutes=0.05,
        interval=5.0,
        threshold=1.0,
        client=lambda _: 0,
        relay_count_fn=lambda: 1,
        load_averages=lambda: (2.0, 1.5, 1.0),
        ansible_timeout_fn=lambda: 10,
        clock=lambda: next(times),
        sleep=lambda _: None,
        timestamp_fn=lambda: next(stamps),
    ).run()

    assert len(records) == 1
    lines = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert lines == [
        {
            "timestamp": "2026-10-24T02:00:01+00:00",
            "call": "docker ps",
            "duration": 1.5,
        },
        {
            "timestamp": "2026-10-24T02:00:11+00:00",
            "call": "docker info",
            "duration": 10.035,
        },
        {
            "timestamp": "2026-10-24T02:00:12+00:00",
            "timings": {
                "docker version": 0.2,
                "docker ps": 1.5,
                "docker info": 10.035,
            },
            "slowest_call": "docker info",
            "slowest_duration": 10.035,
            "relay_process_count": 1,
            "load_averages": [2.0, 1.5, 1.0],
            "ansible_default_timeout": 10,
        },
    ]


def test_ac3_read_only_commands_only_and_starts_no_container(tmp_path: Path) -> None:
    output = tmp_path / "relay.jsonl"
    asked: list[tuple[str, ...]] = []

    def fake_client(argv: relay_diag.Sequence[str]) -> int:
        command = tuple(argv)
        assert command in {
            ("docker", "version"),
            ("docker", "ps"),
            ("docker", "info"),
        }
        asked.append(command)
        return 0

    relay_diag.RelayDiag(
        output=output,
        minutes=0.0,
        interval=5.0,
        client=fake_client,
        relay_count_fn=lambda: 0,
        load_averages=lambda: (0.1, 0.1, 0.1),
        ansible_timeout_fn=lambda: 10,
        clock=lambda: 1.0,
        sleep=lambda _: None,
        timestamp_fn=lambda: "2026-10-24T03:00:00+00:00",
    ).run()

    assert asked == [
        ("docker", "version"),
        ("docker", "ps"),
        ("docker", "info"),
    ]

    docker_runner = Mock(
        return_value=subprocess.CompletedProcess(
            args=["docker"], returncode=0, stdout=""
        )
    )
    for command in relay_diag.READ_ONLY_COMMANDS:
        assert relay_diag.run_docker(command, run=docker_runner) == 0
    assert [call.args[0] for call in docker_runner.call_args_list] == [
        ["docker", "version"],
        ["docker", "ps"],
        ["docker", "info"],
    ]

    with pytest.raises(ValueError, match="refusing non-read-only command"):
        relay_diag.run_docker(["docker", "run", "--rm", "alpine"], run=docker_runner)
    assert len(docker_runner.call_args_list) == 3

    ps_runner = Mock(
        return_value=subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=(
                "\n"
                "/sbin/init\n"
                "docker-relay --socket /var/run/docker.sock\n"
                "pipe-relay.exe -ep -s //./pipe/docker_engine\n"
                "python -m tools.relay_diag --minutes 5\n"
                "mise run relay-diag\n"
            ),
        )
    )
    assert relay_diag.relay_process_count(run=ps_runner) == 2
    assert ps_runner.call_args.args[0] == ["ps", "-eo", "args="]

    cfg_runner = Mock(
        return_value=subprocess.CompletedProcess(
            args=["ansible-config", "dump"],
            returncode=0,
            stdout="DEFAULT_TIMEOUT(default) = 10\n",
        )
    )
    assert relay_diag.ansible_default_timeout(run=cfg_runner) == 10
    assert cfg_runner.call_args.args[0] == ["ansible-config", "dump"]


def test_ac4_cli_and_helpers_with_fake_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    monkeypatch.setenv("CODEX_THREAD_ID", "")

    assert "T" in relay_diag.utc_timestamp()
    assert relay_diag.ansible_default_timeout() == 10

    missing_timeout_runner = Mock(
        return_value=subprocess.CompletedProcess(
            args=["ansible-config", "dump"],
            returncode=0,
            stdout="OTHER_SETTING(default) = 1\n",
        )
    )
    with pytest.raises(ValueError, match="DEFAULT_TIMEOUT"):
        relay_diag.ansible_default_timeout(run=missing_timeout_runner)

    output = tmp_path / "cli-relay.jsonl"
    calls: list[tuple[str, ...]] = []

    def fake_client(argv: relay_diag.Sequence[str]) -> int:
        calls.append(tuple(argv))
        return 0

    exit_code = relay_diag.main(
        ["0", "--interval", "5", "--threshold", "1", "--output", str(output)],
        client=fake_client,
    )
    assert exit_code == 0
    assert calls == [
        ("docker", "version"),
        ("docker", "ps"),
        ("docker", "info"),
    ]
    exit_code_opt = relay_diag.main(
        ["--minutes", "0", "--output", str(output)],
        client=fake_client,
    )
    assert exit_code_opt == 0
    lines = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 2

    root = Path(__file__).resolve().parents[1]
    tasks = tomllib.loads((root / "mise.toml").read_text(encoding="utf-8"))["tasks"]
    assert "relay-diag" in tasks
    assert "python -m tools.relay_diag" in str(tasks["relay-diag"]["run"])


def test_risks_configurable_interval_and_single_pass_per_interval(
    tmp_path: Path,
) -> None:
    output = tmp_path / "risks.jsonl"
    calls: list[tuple[str, ...]] = []
    sleeps: list[float] = []

    records = relay_diag.RelayDiag(
        output=output,
        minutes=0.5,
        interval=12.5,
        threshold=2.0,
        client=lambda argv: calls.append(tuple(argv)) or 0,
        relay_count_fn=lambda: 0,
        load_averages=lambda: (0.2, 0.2, 0.2),
        ansible_timeout_fn=lambda: 10,
        clock=lambda: 5.0,
        sleep=sleeps.append,
        timestamp_fn=lambda: "2026-10-24T04:00:00+00:00",
    ).run()

    assert len(records) == 3
    assert sleeps == [12.5, 12.5]
    assert len(calls) == 9

    with pytest.raises(ValueError, match="interval"):
        relay_diag.RelayDiag(output=output, interval=0.0).run()
    with pytest.raises(ValueError, match="minutes"):
        relay_diag.RelayDiag(output=output, minutes=-1.0).run()
    with pytest.raises(ValueError, match="threshold"):
        relay_diag.RelayDiag(output=output, threshold=-0.1).run()

    with pytest.raises(SystemExit, match="interval"):
        relay_diag.main(
            ["0", "--interval", "0", "--output", str(output)],
            client=lambda _: 0,
        )
