"""Cache the Molecule image by its complete build definition."""

from __future__ import annotations

import hashlib
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def ensure(
    directory: Path,
    *,
    target: str = "base",
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str:
    digest = hashlib.sha256(target.encode())
    for path in sorted(directory.iterdir()):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    image = f"techops-molecule:{digest.hexdigest()}"
    result = run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        run(
            ["docker", "build", "--target", target, "--tag", image, str(directory)],
            check=True,
        )
    return image
