"""Run full or selected Molecule scenarios sequentially with isolated state."""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from tools.gate_image import ensure
from tools.gate_selector import changed_paths, select
from tools.gate_timing import command


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--timings", type=Path, default=None)
    args = parser.parse_args()
    root = Path.cwd()
    try:
        changes = ["shared"] if args.all else changed_paths(root, args.base)
    except subprocess.CalledProcessError:
        changes = ["unknown"]
    scenarios = select(root, changes)
    print("Selected scenarios: " + ", ".join(scenarios), flush=True)
    ansible = root / "infra/ansible"
    if args.timings:
        timings = Path(args.timings)
    else:
        with tempfile.NamedTemporaryFile(
            prefix="gate-timing-", suffix=".tsv", delete=False
        ) as record:
            timings = Path(record.name)
    print(f"Timing record: {timings}", flush=True)
    with tempfile.TemporaryDirectory(prefix="molecule-") as state:
        for scenario in scenarios:
            role, name = scenario.split("/")
            image = ensure(ansible / "molecule/image", target=role)
            env = os.environ | {
                "LC_ALL": "C.UTF-8",
                "ANSIBLE_COLLECTIONS_PATH": str(ansible / ".collections"),
                "ANSIBLE_ROLES_PATH": str(ansible / "roles"),
                "no_proxy": "*",
                "NO_PROXY": "*",
                "MOLECULE_RUN_ID": Path(state).name.removeprefix("molecule-"),
                "MOLECULE_EPHEMERAL_DIRECTORY": f"{state}/{role}/{name}",
                "MOLECULE_IMAGE": image,
            }
            code = command(
                scenario,
                ["uv", "run", "--group", "ops", "molecule", "test", "-s", name],
                timings,
                cwd=ansible / "roles" / role,
                env=env,
            )
            if code:
                raise SystemExit(code)


if __name__ == "__main__":
    main()
