from __future__ import annotations

import subprocess


def docker_available() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
