"""Record read-only Docker call timings and relay state as JSONL."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

READ_ONLY_COMMANDS: tuple[tuple[str, str], ...] = (
    ("docker", "version"),
    ("docker", "ps"),
    ("docker", "info"),
)

DEFAULT_TIMEOUT_RE = re.compile(
    r"^DEFAULT_TIMEOUT(?:\([^)]*\))?\s*=\s*(\d+)", re.MULTILINE
)
RELAY_PROCESS_RE = re.compile(r"relay", re.IGNORECASE)


class SlowCallRecord(TypedDict):
    timestamp: str
    call: str
    duration: float


class IntervalRecord(TypedDict):
    timestamp: str
    timings: dict[str, float]
    slowest_call: str
    slowest_duration: float
    relay_process_count: int
    load_averages: list[float]
    ansible_default_timeout: int


type Client = Callable[[Sequence[str]], int]


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def run_docker(
    argv: Sequence[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    command = tuple(argv)
    if command not in READ_ONLY_COMMANDS:
        msg = f"refusing non-read-only command: {' '.join(command)}"
        raise ValueError(msg)
    result = run(
        list(command),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode


def relay_process_count(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    result = run(
        ["ps", "-eo", "args="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
    )
    count = 0
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or "relay_diag" in line or "relay-diag" in line:
            continue
        if RELAY_PROCESS_RE.search(line):
            count += 1
    return count


def ansible_default_timeout(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    result = run(
        ["ansible-config", "dump"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=True,
        env=os.environ
        | {"LC_ALL": "C.UTF-8", "ANSIBLE_LOCAL_TEMP": tempfile.gettempdir()},
    )
    match = DEFAULT_TIMEOUT_RE.search(result.stdout)
    if not match:
        msg = "DEFAULT_TIMEOUT not found in ansible-config dump"
        raise ValueError(msg)
    return int(match[1])


@dataclass(frozen=True)
class RelayDiag:
    output: Path = field(default_factory=lambda: Path("relay-diag.jsonl"))
    minutes: float = 10.0
    interval: float = 5.0
    threshold: float = 1.0
    client: Client = run_docker
    relay_count_fn: Callable[[], int] = relay_process_count
    load_averages: Callable[[], tuple[float, float, float]] = os.getloadavg
    ansible_timeout_fn: Callable[[], int] = ansible_default_timeout
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    timestamp_fn: Callable[[], str] = utc_timestamp

    def _append(self, record: IntervalRecord | SlowCallRecord) -> None:
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()

    def run(self) -> list[IntervalRecord]:
        if self.interval <= 0:
            msg = "interval must be positive"
            raise ValueError(msg)
        if self.minutes < 0:
            msg = "minutes must not be negative"
            raise ValueError(msg)
        if self.threshold < 0:
            msg = "threshold must not be negative"
            raise ValueError(msg)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        budget = self.minutes * 60.0
        elapsed = 0.0
        records: list[IntervalRecord] = []
        while True:
            timings: dict[str, float] = {}
            for command in READ_ONLY_COMMANDS:
                label = " ".join(command)
                started = self.clock()
                self.client(command)
                duration = round(self.clock() - started, 6)
                timings[label] = duration
                if duration > self.threshold:
                    slow_record: SlowCallRecord = {
                        "timestamp": self.timestamp_fn(),
                        "call": label,
                        "duration": duration,
                    }
                    self._append(slow_record)
            slowest_call, slowest_duration = max(
                timings.items(), key=lambda item: item[1]
            )
            record: IntervalRecord = {
                "timestamp": self.timestamp_fn(),
                "timings": timings,
                "slowest_call": slowest_call,
                "slowest_duration": slowest_duration,
                "relay_process_count": self.relay_count_fn(),
                "load_averages": [float(value) for value in self.load_averages()],
                "ansible_default_timeout": self.ansible_timeout_fn(),
            }
            self._append(record)
            records.append(record)
            elapsed += sum(timings.values())
            if elapsed + self.interval >= budget:
                break
            self.sleep(self.interval)
            elapsed += self.interval
        return records


def main(
    argv: Sequence[str] | None = None,
    *,
    client: Client = run_docker,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("minutes_pos", nargs="?", type=float, metavar="minutes")
    parser.add_argument("-m", "--minutes", dest="minutes_opt", type=float, default=None)
    parser.add_argument("-i", "--interval", type=float, default=5.0)
    parser.add_argument("-t", "--threshold", type=float, default=1.0)
    parser.add_argument(
        "-o",
        "--output",
        "--results",
        dest="output",
        type=Path,
        default=Path("relay-diag.jsonl"),
    )
    args = parser.parse_args(argv)
    minutes: float = (
        args.minutes_opt
        if args.minutes_opt is not None
        else (args.minutes_pos if args.minutes_pos is not None else 10.0)
    )
    try:
        RelayDiag(
            output=args.output,
            minutes=minutes,
            interval=args.interval,
            threshold=args.threshold,
            client=client,
        ).run()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
