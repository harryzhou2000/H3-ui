from __future__ import annotations

import shutil
import subprocess
import sys


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("Node.js is required to validate JavaScript", file=sys.stderr)
        return 1
    return max(
        (
            subprocess.run([node, "--check", filename], check=False).returncode
            for filename in sys.argv[1:]
        ),
        default=0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
