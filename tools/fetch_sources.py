#!/usr/bin/env python3
"""Fetch pinned public tool sources. No binaries or disk images are downloaded."""
import argparse
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, output, require, run

SOURCES = {
    'mkbootimg': ('https://android.googlesource.com/platform/system/tools/mkbootimg', 'd2bb0af5ba6d3198a3e99529c97eda1be0b5a093'),
    'avb': ('https://android.googlesource.com/platform/external/avb', 'c5066a96caa7bf4150c0a8cc8cc14ab81733fdc7'),
    'vboot': ('https://chromium.googlesource.com/chromiumos/platform/vboot_reference', '7cf41276406903650ee34d4f16af74196c85d346'),
}


def fetch_sources(destination, runner=run):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, (url, revision) in SOURCES.items():
        dest = destination / name
        require(not dest.is_symlink(), name + ': refusing a symlinked source directory')
        if not dest.exists():
            runner(['git', 'init', str(dest)])
        require((dest / '.git').is_dir(), name + ': source directory is not a Git checkout')
        remotes = output(['git', '-C', dest, 'remote']).splitlines()
        if 'origin' not in remotes:
            require(not any(p.name != '.git' for p in dest.iterdir()), name + ': existing source has no origin')
            runner(['git', '-C', dest, 'remote', 'add', 'origin', url])
        require(output(['git', '-C', dest, 'remote', 'get-url', 'origin']) == url, name + ': wrong source origin')
        head = subprocess.run(['git', '-C', str(dest), 'rev-parse', '--verify', 'HEAD'],
                              capture_output=True, text=True)
        if head.returncode:
            # A network failure may leave an initialized but unpopulated checkout.
            require(not any(p.name != '.git' for p in dest.iterdir()), name + ': unfinished checkout contains files')
            runner(['git', '-C', dest, 'fetch', '--progress', '--depth=1', 'origin', revision])
            runner(['git', '-C', dest, 'checkout', '--detach', revision])
        require(output(['git', '-C', dest, 'rev-parse', 'HEAD']) == revision, f'{name}: wrong revision')
        require(not output(['git', '-C', dest, 'status', '--porcelain', '--untracked-files=no']), f'{name}: modified source')
        print(f'{name}: verified {revision}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination', type=Path, default=ROOT / 'build/sources')
    args = p.parse_args()
    fetch_sources(args.destination)


if __name__ == '__main__':
    main()
