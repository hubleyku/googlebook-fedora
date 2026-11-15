"""Small, explicit helpers shared by the image builder and target tools."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def runtime_sources(root=ROOT):
    """Use bundled tools in an installed kit, fetched sources in a checkout."""
    root = Path(root)
    bundled = root / 'vendor'
    return bundled if bundled.is_dir() else root / 'build/sources'


def require(condition, message):
    # Never use assert for a guard: Python -O must not disable it.
    if not condition:
        raise RuntimeError(message)


def sha256(path, progress=None):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        total, done = os.fstat(f.fileno()).st_size, 0
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
            done += len(block)
            if progress:
                progress(done, total)
    return h.hexdigest()


def run(argv, **kwargs):
    # Passwords are passed on stdin, never in argv or this log.
    print('+', ' '.join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def output(argv):
    return subprocess.check_output(list(map(str, argv)), text=True).strip()


def save_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def new_regular_output(path):
    path = Path(path)
    require(not path.exists() and not path.is_symlink(), f'Output already exists: {path}')
    require(not str(path.resolve()).startswith('/dev/'), 'Image tools never write device nodes')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def require_regular(path):
    path = Path(path)
    require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), f'Not a regular file: {path}')


def read_json(path):
    return json.loads(Path(path).read_text())
