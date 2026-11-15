from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from lapis import fedora_target, locales
from lapis.common import read_json
from tools import configure_fedora as target, install_fedora as installer


class LocaleTests(unittest.TestCase):
    def test_utf8_names_and_language_packages(self):
        for supplied, expected in [('en_US', 'en_US.UTF-8'), ('fr_CA.utf8', 'fr_CA.UTF-8'),
                                   ('sr_RS.UTF-8@latin', 'sr_RS.UTF-8@latin'), ('C.utf8', 'C.UTF-8')]:
            with self.subTest(supplied=supplied):
                self.assertEqual(locales.validate_locale(supplied), expected)
        self.assertEqual(locales.locale_packages('fr_CA'), ['glibc-langpack-fr'])
        self.assertEqual(locales.locale_packages('C.UTF-8'), [])
        for invalid in ['../../en_US', 'en_US.ISO-8859-1', 'en_US\nLC_ALL=C', 'en_US;bad', 'C.UTF-8@latin', '']:
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                locales.validate_locale(invalid)

    def test_selected_language_data_is_checked_before_installation(self):
        packages = [p for p in target.PACKAGES if not p.startswith('@')]
        available = '\n'.join('kernel' if p.startswith('kernel-') else p for p in packages)
        for data, succeeds in [('/usr/lib/locale/fr_CA.utf8/LC_CTYPE\n', True),
                               ('/usr/lib/locale/fr_FR.utf8/LC_CTYPE\n', False)]:
            with self.subTest(succeeds=succeeds):
                results = [subprocess.CompletedProcess([], 0, available + '\nglibc-langpack-fr\n'),
                           subprocess.CompletedProcess([], 0, data), subprocess.CompletedProcess([], 0)]
                with patch.object(installer.subprocess, 'run', side_effect=results) as query:
                    if succeeds:
                        installer.verify_package_sources('none', 'fr_CA.UTF-8')
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'does not provide'):
                            installer.verify_package_sources('none', 'fr_CA.UTF-8')
                for call in query.call_args_list:
                    self.assertIn('--releasever=45', call.args[0])

    def test_target_locale_verification_accepts_utf8_alias_and_rejects_missing_data(self):
        result = subprocess.CompletedProcess([], 0, 'C\nC.utf8\nen_US\nfr_CA.utf8\n')
        with patch.object(locales.subprocess, 'run', return_value=result):
            locales.verify_target_locale('/target', 'fr_CA.UTF-8')
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                locales.verify_target_locale('/target', 'en_US.UTF-8')

    def test_configuration_writes_default_and_selected_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zone = root / 'usr/share/zoneinfo/UTC'
            zone.parent.mkdir(parents=True)
            zone.write_bytes(b'test timezone')
            fedora_target.configure(root, str(uuid.uuid4()), 'UTC')
            self.assertEqual((root / 'etc/locale.conf').read_text(), 'LANG=en_US.UTF-8\n')
            fedora_target.configure(root, str(uuid.uuid4()), 'UTC', locale='fr_CA')
            self.assertEqual((root / 'etc/locale.conf').read_text(), 'LANG=fr_CA.UTF-8\n')


class ProvisioningTests(unittest.TestCase):
    def exercise_packages(self, *, fail_account=False, desktop='none'):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp) / 'target'
            state = Path(tmp) / 'state'
            root.mkdir(); state.mkdir()
            stack.enter_context(patch.object(target, 'TARGET', root))
            stack.enter_context(patch.object(target, 'STATE', state))
            stack.enter_context(patch.object(target, 'current'))
            stack.enter_context(patch.object(target, 'mounted'))
            stack.enter_context(patch.object(target, 'checked_backup'))
            stack.enter_context(patch.object(target, 'read_json', return_value={'device': '/dev/example'}))
            stack.enter_context(patch.object(target.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)))
            events = []
            bound = set()

            def command(argv):
                events.append(argv)
                if argv[:2] == ['mount', '--rbind']:
                    bound.add(argv[-1])
                elif argv[:2] == ['umount', '-R']:
                    bound.remove(argv[-1])

            stack.enter_context(patch.object(target, 'run', side_effect=command))
            stack.enter_context(patch.object(target, 'install_desktop'))
            verify = stack.enter_context(patch.object(target, 'verify_target_locale'))

            def account(*args):
                self.assertEqual(bound, {root / name for name in ('dev', 'sys', 'proc')})
                self.assertIn(['chroot', root, 'authselect', 'check'], events)
                if fail_account:
                    raise RuntimeError('account failed')

            stack.enter_context(patch.object(target, 'create_account', side_effect=account))
            args = SimpleNamespace(user='installtest', timezone='UTC', keymap='us', hostname='test',
                                   desktop=desktop, locale='fr_CA.UTF-8', apply=True)
            if fail_account:
                with self.assertRaisesRegex(RuntimeError, 'account failed'):
                    target.packages(args, password='secret')
                self.assertFalse((state / 'packages.json').exists())
            else:
                target.packages(args, password='secret')
                self.assertEqual(read_json(state / 'account.json')['locale'], 'fr_CA.UTF-8')
                verify.assert_called_once_with(root, 'fr_CA.UTF-8')
            self.assertEqual(bound, set())
            self.assertEqual([c[-1] for c in events if c[:2] == ['umount', '-R']],
                             [root / name for name in ('proc', 'sys', 'dev')])
            install = next(c for c in events if c[0] == 'dnf5')
            self.assertIn('glibc-langpack-fr', install)
            self.assertEqual('--exclude=kmscon*' in install, desktop == 'none')

    def test_mounts_remain_until_account_setup_finishes(self):
        self.exercise_packages()

    def test_failed_account_setup_cleans_pseudo_mounts_without_marking_success(self):
        self.exercise_packages(fail_account=True)

    def test_graphical_install_keeps_its_package_choices(self):
        self.exercise_packages(desktop='kde')

    @unittest.skipUnless(shutil.which('systemctl'), 'systemctl is required')
    def test_console_keeps_standard_getty_and_supports_later_greetd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = root / 'usr/lib/systemd/system'
            units.mkdir(parents=True)
            (root / 'etc/systemd/system/getty.target.wants').mkdir(parents=True)
            (units / 'getty@.service').write_text('[Service]\nExecStart=/usr/sbin/agetty\n'
                                                '[Install]\nAlias=autovt@.service\nWantedBy=getty.target\nDefaultInstance=tty1\n')
            (units / 'multi-user.target').write_text('[Unit]\nDescription=Console\n')
            (units / 'graphical.target').write_text('[Unit]\nDescription=Graphical\n'
                                                   'Requires=multi-user.target\nWants=display-manager.service\n')
            (units / 'getty.target').write_text('[Unit]\nDescription=Console logins\n')
            (root / 'etc/systemd/system/autovt@.service').symlink_to('/usr/lib/systemd/system/kmsconvt@.service')
            with patch.object(target, 'TARGET', root):
                target.configure_desktop('none')
            system = root / 'etc/systemd/system'
            self.assertEqual((system / 'autovt@.service').readlink(), Path('/usr/lib/systemd/system/getty@.service'))
            self.assertTrue((system / 'getty.target.wants/getty@tty1.service').is_symlink())
            for name in ('kmscon.service', 'kmsconvt@.service'):
                self.assertEqual((system / name).readlink(), Path('/dev/null'))
            self.assertEqual((system / 'default.target').readlink(), Path('/usr/lib/systemd/system/graphical.target'))
            preset = (root / 'etc/systemd/system-preset/00-lapis-console.preset').read_text()
            self.assertIn('disable kmsconvt@.service', preset)
            self.assertIn('enable getty@.service', preset)
            self.assertFalse((system / 'display-manager.service').is_symlink())
            # Fedora greetd enables only this alias, with no multi-user Wants.
            # Enabling it after a console install must be enough for next boot.
            (units / 'greetd.service').write_text('[Service]\nExecStart=/usr/bin/greetd\n'
                                                '[Install]\nAlias=display-manager.service\n')
            subprocess.run(['systemctl', '--root=' + str(root), 'enable', 'greetd.service'], check=True)
            self.assertEqual((system / 'display-manager.service').readlink(), Path('/usr/lib/systemd/system/greetd.service'))
            default = subprocess.check_output(['systemctl', '--root=' + str(root), 'get-default'], text=True).strip()
            self.assertEqual(default, 'graphical.target')
            self.assertIn('Wants=display-manager.service', (units / default).read_text())


if __name__ == '__main__':
    unittest.main()
