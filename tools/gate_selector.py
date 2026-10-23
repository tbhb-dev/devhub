"""Select scenarios conservatively from roles and the playbooks they execute."""

import argparse
import subprocess
from pathlib import Path
from typing import cast

import yaml

ANSIBLE = Path("infra/ansible")
ROLE_PREFIX = (*ANSIBLE.parts, "roles")


def name(value: object) -> str:
    if not isinstance(value, str) or "{{" in value:
        raise ValueError
    return value


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError
    return cast("dict[str, object]", value)


def references(path: Path, seen: set[Path] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return set()
    seen.add(path)
    result: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, list):
            for item in cast("list[object]", value):
                walk(item)
        elif isinstance(value, dict):
            for key, item in mapping(value).items():
                short = str(key).rsplit(".", 1)[-1]
                if short in {"roles", "dependencies"}:
                    if not isinstance(item, list):
                        raise TypeError
                    for role in cast("list[object]", item):
                        result.add(
                            name(
                                role if isinstance(role, str) else mapping(role)["role"]
                            )
                        )
                elif short in {"include_role", "import_role"}:
                    result.add(name(mapping(item)["name"]))
                elif short == "import_playbook":
                    result.update(references(path.parent / name(item), seen))
                elif short in {"include_tasks", "import_tasks"}:
                    target = item if isinstance(item, str) else mapping(item)["file"]
                    result.update(references(path.parent / name(target), seen))
                else:
                    walk(item)

    walk(yaml.safe_load(path.read_text()))
    return result


def select(root: Path, changes: list[str]) -> list[str]:
    if not changes:
        return []
    ansible = root / ANSIBLE
    scenarios = {
        f"{path.parents[1].name}/{path.name}": path
        for path in (ansible / "roles").glob("*/molecule/*")
        if path.is_dir()
    }
    everything = sorted(scenarios)
    roles = {path.name for path in (ansible / "roles").iterdir() if path.is_dir()}
    selected: set[str] = set()
    try:
        dependencies = {
            role: references(ansible / "roles" / role / "meta/main.yml")
            for role in roles
        }
        uses = {
            name: references(path / "converge.yml") | {name.split("/")[0]}
            for name, path in scenarios.items()
        }
        if any(needs - roles for needs in (*dependencies.values(), *uses.values())):
            return everything
        for name in changes:
            path = Path(name)
            parts = path.parts
            if parts[: len(ROLE_PREFIX)] == ROLE_PREFIX and len(parts) > len(
                ROLE_PREFIX
            ):
                affected = {parts[len(ROLE_PREFIX)]}
            elif path.parent == ANSIBLE / "playbooks" and (root / path).is_file():
                affected = references(root / path)
            else:
                # Shared infrastructure and every unmapped file use the full suite.
                return everything
            if not affected or affected - roles:
                return everything
            while True:
                expanded = affected | {
                    role for role, needs in dependencies.items() if needs & affected
                }
                if expanded == affected:
                    break
                affected = expanded
            selected.update(
                name for name, applied in uses.items() if applied & affected
            )
    except OSError, ValueError, KeyError, TypeError, yaml.YAMLError:
        # Deleted files, dynamic role names and unfamiliar YAML must select all.
        return everything
    return sorted(selected)


def changed_paths(root: Path, base: str) -> list[str]:
    merge = subprocess.check_output(
        ["git", "merge-base", base, "HEAD"], cwd=root, text=True
    ).strip()
    paths = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", merge], cwd=root
    )
    paths += subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root
    )
    return sorted(set(paths.decode().strip("\0").split("\0")) - {""})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    args = parser.parse_args()
    root = Path.cwd()
    try:
        names = select(root, changed_paths(root, args.base))
    except subprocess.CalledProcessError:
        names = select(root, ["unknown"])
    print("\n".join(names))


if __name__ == "__main__":
    main()
