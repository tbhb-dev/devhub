"""Run and resume a Molecule scenario soak with per-run JSONL records."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from tools.gate_image import ensure
from tools.gate_molecule import molecule_argv, scenario_env

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TASK_RE = re.compile(r"^(?:TASK|RUNNING HANDLER)\s*\[(.+?)\]")
FAIL_RE = re.compile(r"^(?:fatal|failed|ERROR!|CRITICAL)\b")


class RunRecord(TypedDict):
    run: int
    run_id: str
    status: str
    wall_seconds: float
    load_start: list[float]
    load_finish: list[float]
    commit: str
    failing_task: str | None
    error_line: str | None


type Runner = Callable[[str, Path, Path], tuple[int, str]]


def head_commit(root: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


def parse_failure(output: str) -> tuple[str | None, str | None]:
    lines = [ANSI_RE.sub("", raw).strip() for raw in output.splitlines()]
    current_task: str | None = None
    fallback: str | None = None
    for index, line in enumerate(lines):
        if not line:
            continue
        task_match = TASK_RE.match(line)
        if task_match:
            current_task = task_match[1]
            continue
        if FAIL_RE.match(line):
            next_lines = [item for item in lines[index + 1 :] if item]
            if next_lines and next_lines[0] == "...ignoring":
                continue
            return current_task, line
        if not line.startswith(("PLAY ", "ok:", "changed:", "skipping:")):
            fallback = line
    return current_task, fallback


def run_scenario(
    scenario: str,
    root: Path,
    state: Path,
    *,
    ensure_image: Callable[..., str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[int, str]:
    role, name = scenario.split("/", 1) if "/" in scenario else (scenario, "default")
    ansible = root / "infra/ansible"
    image = (ensure_image or ensure)(ansible / "molecule/image", target=role)
    env = scenario_env(ansible, scenario, state, image)
    result = run(
        molecule_argv(name),
        cwd=ansible / "roles" / role,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout or ""
    if output:
        print(output, end="", flush=True)
    return result.returncode, output


def load_existing(results: Path, commit: str, *, start_over: bool) -> list[RunRecord]:
    if start_over:
        if results.exists():
            results.unlink()
        return []
    if not results.exists():
        return []
    records: list[RunRecord] = []
    for raw in results.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        item = cast("RunRecord", json.loads(line))
        if item.get("commit") != commit:
            msg = (
                f"refusing to resume: results commit {item.get('commit')} "
                f"differs from HEAD {commit}; pass --start-over to reset"
            )
            raise ValueError(msg)
        records.append(item)
    return records


@dataclass(frozen=True)
class Soak:
    scenario: str
    count: int
    results: Path
    max_failures: int = 3
    start_over: bool = False
    runner: Runner = run_scenario
    root: Path = field(default_factory=Path.cwd)
    load_averages: Callable[[], tuple[float, float, float]] = os.getloadavg
    clock: Callable[[], float] = time.monotonic
    commit_fn: Callable[[Path], str] | None = None

    def run(self) -> list[RunRecord]:
        commit = (self.commit_fn or head_commit)(self.root)
        records = load_existing(self.results, commit, start_over=self.start_over)
        self.results.parent.mkdir(parents=True, exist_ok=True)
        consecutive_failures = 0
        for item in reversed(records):
            if item["status"] == "fail":
                consecutive_failures += 1
            else:
                break
        for run_number in range(len(records) + 1, self.count + 1):
            if self.max_failures > 0 and consecutive_failures >= self.max_failures:
                break
            with tempfile.TemporaryDirectory(prefix="molecule-") as state:
                state_path = Path(state)
                run_id = state_path.name.removeprefix("molecule-")
                load_start = [float(value) for value in self.load_averages()]
                started = self.clock()
                returncode, output = self.runner(self.scenario, self.root, state_path)
                wall_seconds = round(self.clock() - started, 6)
                load_finish = [float(value) for value in self.load_averages()]
            if returncode == 0:
                status = "pass"
                failing_task = None
                error_line = None
                consecutive_failures = 0
            else:
                status = "fail"
                failing_task, error_line = parse_failure(output)
                consecutive_failures += 1
            record: RunRecord = {
                "run": run_number,
                "run_id": run_id,
                "status": status,
                "wall_seconds": wall_seconds,
                "load_start": load_start,
                "load_finish": load_finish,
                "commit": commit,
                "failing_task": failing_task,
                "error_line": error_line,
            }
            with self.results.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
                stream.flush()
            records.append(record)
        return records


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = run_scenario,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario_pos", nargs="?", metavar="scenario")
    parser.add_argument("count_pos", nargs="?", type=int, metavar="runs")
    parser.add_argument("-s", "--scenario", dest="scenario_opt")
    parser.add_argument(
        "-n", "--runs", "--count", dest="count_opt", type=int, default=None
    )
    parser.add_argument(
        "-o",
        "--results",
        "--output",
        type=Path,
        default=Path("soak-results.jsonl"),
    )
    parser.add_argument(
        "--max-consecutive-failures",
        "--max-failures",
        dest="max_failures",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--start-over",
        "--reset",
        dest="start_over",
        action="store_true",
    )
    args = parser.parse_args(argv)
    scenario: str | None = args.scenario_opt or args.scenario_pos
    if not scenario:
        parser.error("a scenario name is required")
    count: int = (
        args.count_opt
        if args.count_opt is not None
        else (args.count_pos if args.count_pos is not None else 20)
    )
    try:
        records = Soak(
            scenario=scenario,
            count=count,
            results=args.results,
            max_failures=args.max_failures,
            start_over=args.start_over,
            runner=runner,
        ).run()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 1 if any(item["status"] == "fail" for item in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
