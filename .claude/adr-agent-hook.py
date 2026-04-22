#!/usr/bin/env python3
import shutil
import subprocess
import sys


def main():
    args = sys.argv[1:]
    stdin_data = sys.stdin.buffer.read()

    if shutil.which("adr-agent"):
        sys.exit(subprocess.run(["adr-agent"] + args, input=stdin_data).returncode)

    if shutil.which("uv"):
        r = subprocess.run(["uv", "run", "adr-agent"] + args, input=stdin_data)
        if r.returncode == 0:
            sys.exit(0)

    print(
        "adr-agent not installed; skipping hook. "
        "Install with: pip install adr-agent",
        file=sys.stderr,
    )
    sys.exit(0)


main()
