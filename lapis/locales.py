"""UTF-8 locale preferences and Fedora language data requirements."""
from pathlib import Path
import re
import subprocess

from .common import require

DEFAULT_LOCALE = 'en_US.UTF-8'


def validate_locale(value):
    match = re.fullmatch(r'(C|[a-z]{2,3}(?:_[A-Z]{2}|_[0-9]{3})?)(?:\.(?i:utf-?8))?(@[a-z0-9_-]+)?', value)
    require(match is not None, 'Enter a UTF-8 locale such as en_US.UTF-8 or fr_CA.UTF-8.')
    require(match[1] != 'C' or match[2] is None, 'C.UTF-8 does not support locale modifiers.')
    return match[1] + '.UTF-8' + (match[2] or '')


def locale_packages(value):
    value = validate_locale(value)
    language = value.split('.', 1)[0].split('_', 1)[0]
    return [] if language == 'C' else ['glibc-langpack-' + language]


def locale_data_path(value):
    return '/usr/lib/locale/' + validate_locale(value).replace('.UTF-8', '.utf8') + '/LC_CTYPE'


def available_locales():
    # This is a list of suggestions, not a restriction to the live ISO's
    # installed languages. Fedora metadata validates the chosen data pre-erase.
    values = {DEFAULT_LOCALE, 'C.UTF-8'}
    result = subprocess.run(['locale', '-a'], capture_output=True, text=True)
    candidates = result.stdout.splitlines() if result.returncode == 0 else []
    candidates.extend(p.name for p in Path('/usr/share/i18n/locales').glob('*'))
    for candidate in candidates:
        try:
            values.add(validate_locale(candidate))
        except RuntimeError:
            pass
    return values


def verify_target_locale(root, value):
    result = subprocess.run(['chroot', str(root), 'locale', '-a'],
                            capture_output=True, text=True, check=True)
    installed = set()
    for name in result.stdout.splitlines():
        # Bare names in locale -a can refer to non-UTF-8 locales.
        if '.' not in name:
            continue
        try:
            installed.add(validate_locale(name))
        except RuntimeError:
            pass
    require(validate_locale(value) in installed, 'Selected locale is unavailable in target: ' + value)
