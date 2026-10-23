"""Record command and Molecule phase wall times in the baseline TSV format."""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class Record:
    def __init__(self, path: Path) -> None:
        self.path: Path = path

    def event(self, name: str, phase: str, event: str) -> None:
        with self.path.open("a") as stream:
            stream.write(f"{name}\t{phase}\t{event}\t{time.time():.6f}\n")


def command(
    name: str,
    argv: list[str],
    path: Path,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    record = Record(path)
    record.event(name, "-", "START")
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=env,
    ) as process:
        if process.stdout is not None:
            for line in process.stdout:
                print(line, end="", flush=True)
                clean = re.sub(r"\x1b\[[0-9;]*m", "", line)
                match = re.search(r"> ([a-z_]+)\] Executing", clean)
                if match:
                    record.event(name, match[1], "PHASE")
        code = process.wait()
    record.event(name, "-", "END" if code == 0 else "FAILED")
    return code


def summarize(lines: Iterable[str]) -> dict[str, dict[str, float | None]]:
    results: dict[str, dict[str, float | None]] = {}
    previous: dict[str, tuple[str, float]] = {}
    starts: dict[str, float] = {}
    for line in lines:
        name, phase, event, raw_epoch, *_ = line.strip().split("\t")
        epoch = float(raw_epoch)
        if name == "ALL":
            continue
        result = results.setdefault(name, {"total": None})
        if name in previous:
            old_phase, old_epoch = previous.pop(name)
            result[old_phase] = (result.get(old_phase) or 0.0) + epoch - old_epoch
        if event == "START":
            starts[name] = epoch
        elif event == "PHASE":
            previous[name] = (phase, epoch)
        elif event == "END":
            result["total"] = epoch - starts[name]
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("name", nargs="?")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.summary:
        for name, values in summarize(args.summary.read_text().splitlines()).items():
            print(name, values)
    elif args.record and args.name and args.command:
        raise SystemExit(command(args.name, args.command, args.record))
    else:
        parser.error("choose --summary or --record with a name and command")


if __name__ == "__main__":
    main()
