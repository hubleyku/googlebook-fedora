#!/usr/bin/env python3
"""Mount the installer USB's dedicated backup filesystem after live boot."""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.installer_usb import ensure_mounted


def main():
    try:
        print('Installer backup storage:', ensure_mounted())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print('Installer USB storage unavailable: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
