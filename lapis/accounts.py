"""Provision shadow passwords without running the chroot's PAM password stack."""
import ctypes
import ctypes.util
import hmac
import os
from pathlib import Path
import subprocess

from .common import require, run
from .fedora_target import validate_account


def crypt_library():
    library = ctypes.CDLL(ctypes.util.find_library('crypt') or 'libcrypt.so.2', use_errno=True)
    library.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    library.crypt.restype = ctypes.c_char_p
    library.crypt_gensalt_rn.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p,
                                       ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    library.crypt_gensalt_rn.restype = ctypes.c_char_p
    return library


def hash_password(password):
    require(bool(password) and not any(c in password for c in ('\n', '\r', '\0')), 'Password must not be empty or contain line breaks.')
    require(len(password.encode()) <= 512, 'Use a password no longer than 512 UTF-8 bytes.')
    library = crypt_library()
    random = os.urandom(32)
    output = ctypes.create_string_buffer(256)
    # Fedora uses yescrypt. SHA-512 crypt remains available for build/live
    # environments whose libcrypt lacks yescrypt; both bypass PAM setup.
    salt = library.crypt_gensalt_rn(b'$y$', 0, random, len(random), output, len(output))
    if not salt:
        salt = library.crypt_gensalt_rn(b'$6$', 100000, random, len(random), output, len(output))
    require(bool(salt), 'Unable to generate a password salt')
    encoded = library.crypt(password.encode(), salt)
    require(encoded and not encoded.startswith(b'*'), 'Unable to hash the password')
    return encoded.decode('ascii')


def verify_password(password, encoded):
    value = crypt_library().crypt(password.encode(), encoded.encode('ascii'))
    return bool(value) and hmac.compare_digest(value, encoded.encode('ascii'))


def create_account(root, user, password):
    root = Path(root)
    require(not root.is_symlink() and root.is_dir() and root.resolve() != Path('/'), 'Expected a separate target root')
    root = root.resolve()
    validate_account(user, password)
    encoded = hash_password(password)
    run(['chroot', root, 'useradd', '-m', '-G', 'wheel', '-s', '/bin/bash', user])
    # Run the live system's shadow-utils against the offline target. Together,
    # --prefix and --encrypted avoid the target's PAM/SELinux runtime during
    # provisioning. Target SELinux labels are applied later by label_target().
    # Only the hash is supplied, over stdin; never log it or the plaintext.
    result = subprocess.run(['chpasswd', '--prefix', str(root), '--encrypted'],
                            input=(user + ':' + encoded + '\n').encode(), capture_output=True)
    # Preserve diagnostics, but defensively redact either form of the secret.
    detail = (result.stderr or b'').decode(errors='replace').replace(encoded, '[redacted]').replace(password, '[redacted]').strip()
    require(result.returncode == 0, 'Unable to write the target account password' + (': ' + detail if detail else ''))
    records = [line.split(':') for line in (root / 'etc/shadow').read_text().splitlines()]
    stored = next((r[1] for r in records if r[0] == user), None)
    require(stored == encoded and verify_password(password, stored), 'Target password verification failed')
    run(['chroot', root, 'usermod', '--lock', 'root'])
