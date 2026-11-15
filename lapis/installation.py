"""Installer preferences and the single authorization for a planned install."""
from dataclasses import dataclass, field
import pwd
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import require
from .fedora_target import validate_account
from .locales import DEFAULT_LOCALE, validate_locale


@dataclass(frozen=True)
class Desktop:
    name: str
    environment: str | None
    managers: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()


DESKTOPS = {
    'none': Desktop('None (console only)', None),
    'gnome': Desktop('GNOME', 'workstation-product-environment', ('gdm',), ('gdm',)),
    'kde': Desktop('KDE Plasma', 'kde-desktop-environment', ('plasmalogin', 'sddm'), ('plasma-login-manager',)),
    'xfce': Desktop('Xfce', 'xfce-desktop-environment', ('lightdm',), ('lightdm',)),
    'cinnamon': Desktop('Cinnamon', 'cinnamon-desktop-environment', ('lightdm',), ('lightdm',)),
    'mate': Desktop('MATE', 'mate-desktop-environment', ('lightdm',), ('lightdm',)),
    'lxqt': Desktop('LXQt', 'lxqt-desktop-environment', ('sddm',), ('sddm',)),
    'budgie': Desktop('Budgie', 'budgie-desktop-environment', ('sddm', 'lightdm', 'gdm'), ('sddm',)),
    'cosmic': Desktop('COSMIC', 'cosmic-desktop-environment', ('cosmic-greeter',), ('cosmic-greeter',)),
}


def validate_timezone(value):
    require(re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*', value),
            'Enter a timezone such as America/Halifax or UTC.')
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise RuntimeError('Unknown timezone. Enter a name such as America/Halifax or UTC.') from None
    return value


def validate_username(value):
    validate_account(value, 'validation-only')
    reserved = {'gdm', 'sddm', 'lightdm', 'plasmalogin', 'cosmic-greeter', 'polkitd',
                'rtkit', 'avahi', 'dbus', 'chrony', 'sshd', 'dnsmasq', 'geoclue'}
    require(value not in reserved, 'That username is reserved for a system service.')
    try:
        account = pwd.getpwnam(value)
    except KeyError:
        pass
    else:
        require(account.pw_uid >= 1000, 'That username is reserved for a system account.')
    return value


@dataclass(frozen=True)
class Preferences:
    timezone: str
    keymap: str
    user: str
    password: str = field(repr=False)
    desktop: str = 'none'
    hostname: str = 'googlebook'
    locale: str = DEFAULT_LOCALE

    def validate(self, keymaps):
        validate_timezone(self.timezone)
        validate_locale(self.locale)
        require(self.keymap in keymaps, 'Unknown keymap; enter ? to list available keymaps.')
        validate_account(self.user, self.password)
        require(self.desktop in DESKTOPS, 'Choose one of the listed desktop environments.')
        require(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', self.hostname), 'Invalid hostname')

    def public(self):
        return dict(timezone=self.timezone, keymap=self.keymap, user=self.user,
                    desktop=self.desktop, hostname=self.hostname, locale=validate_locale(self.locale))


@dataclass(frozen=True)
class InstallApproval:
    """An in-process YES acknowledgment bound to an unchanged installation plan."""
    device: str
    disk_guid: str
    gpt_sha256: str

    @classmethod
    def for_plan(cls, plan):
        return cls(plan['device'], plan['disk_guid'], plan['gpt_sha256'])

    def check(self, plan):
        require(self == self.for_plan(plan), 'Target disk or partition table changed after YES')
