import configparser
from contextlib import nullcontext
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

from lapis import accounts, network
from tools import configure_fedora


class AccountsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('chpasswd'), 'shadow-utils is required')
    def test_actual_encrypted_shadow_update_in_a_temporary_prefix(self):
        with tempfile.TemporaryDirectory(prefix='lapis-shadow-test-') as tmp:
            root = Path(tmp)
            (root / 'etc').mkdir()
            (root / 'etc/passwd').write_text('installtest:x:1000:1000::/home/installtest:/bin/bash\n')
            shadow = root / 'etc/shadow'
            shadow.write_text('installtest:!:20000:0:99999:7:::\n')
            shadow.chmod(0o600)
            secret = 'Real encrypted shadow test $ café'
            encoded = accounts.hash_password(secret)
            result = subprocess.run(['chpasswd', '--prefix', str(root), '--encrypted'],
                                    input=('installtest:' + encoded + '\n').encode(), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            stored = shadow.read_text().split(':')[1]
            self.assertEqual(stored, encoded)
            self.assertTrue(accounts.verify_password(secret, stored))
            self.assertFalse(accounts.verify_password('incorrect', stored))

    def test_salted_hash_accepts_correct_password_and_rejects_wrong_password(self):
        password = 'Test $ password: café \\ end'
        encoded = accounts.hash_password(password)
        self.assertNotEqual(encoded, accounts.hash_password(password))
        self.assertTrue(accounts.verify_password(password, encoded))
        self.assertFalse(accounts.verify_password('incorrect', encoded))
        for bad in ('', 'line\nbreak', 'nul\0byte', 'a' * 513):
            with self.subTest(bad_length=len(bad)), self.assertRaises(RuntimeError):
                accounts.hash_password(bad)

    def test_offline_password_is_encrypted_and_verified_without_plaintext_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'etc').mkdir()
            secret = 'A private test password'
            def write_shadow(command, *, input, capture_output):
                self.assertEqual(command, ['chpasswd', '--prefix', str(root.resolve()), '--encrypted'])
                self.assertNotIn(secret.encode(), input)
                (root / 'etc/shadow').write_text(input.decode().strip() + ':20000:0:99999:7:::\n')
                return subprocess.CompletedProcess(command, 0)
            with patch.object(accounts, 'run') as run, patch.object(accounts.subprocess, 'run', side_effect=write_shadow):
                accounts.create_account(root, 'installtest', secret)
                self.assertIn('useradd', run.call_args_list[0].args[0])
                self.assertEqual(run.call_args_list[-1].args[0][-3:], ['usermod', '--lock', 'root'])
                self.assertNotIn(secret, repr(run.call_args_list))
            stored = (root / 'etc/shadow').read_text().split(':')[1]
            self.assertTrue(accounts.verify_password(secret, stored))

    def test_password_update_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(accounts, 'run') as run, \
                    patch.object(accounts.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stderr=b'shadow database is locked')):
                with self.assertRaisesRegex(RuntimeError, 'shadow database is locked'):
                    accounts.create_account(root, 'installtest', 'secret')
                self.assertEqual(run.call_count, 1)

    def test_password_update_diagnostics_redact_plaintext_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            encoded = accounts.hash_password('secret')
            result = subprocess.CompletedProcess([], 1, stderr=('secret ' + encoded).encode())
            with patch.object(accounts, 'run'), patch.object(accounts, 'hash_password', return_value=encoded), \
                    patch.object(accounts.subprocess, 'run', return_value=result):
                with self.assertRaises(RuntimeError) as error:
                    accounts.create_account(tmp, 'installtest', 'secret')
            self.assertNotIn('secret', str(error.exception))
            self.assertNotIn(encoded, str(error.exception))


class WifiTests(unittest.TestCase):
    def profile(self, directory, ident, security):
        (directory / 'wifi.nmconnection').write_text(
            '[connection]\nid=Live WiFi\nuuid=' + ident + '\ntype=wifi\npermissions=user:liveuser:;\n'
            '[wifi]\nssid=Test WiFi\n' + security)

    def test_profiles_secrets_and_permissions_are_transferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live, target = root / 'live', root / 'target'
            live.mkdir(); target.mkdir()
            ident = str(uuid.uuid4())
            self.profile(live, ident, '[wifi-security]\nkey-mgmt=wpa-psk\npsk-flags=1\n')
            profiles = network.read_wifi_profiles((live,), {ident}, secret_reader=lambda *args: ' secret $ pass ')
            self.assertNotIn('secret $ pass', repr(profiles))
            network.install_wifi_profiles(target, profiles)
            path = target / 'etc/NetworkManager/system-connections' / ('lapis-wifi-' + ident + '.nmconnection')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config = configparser.ConfigParser(interpolation=None)
            config.read_string(path.read_text())
            self.assertEqual(config['connection']['permissions'], '')
            self.assertEqual(config['wifi-security']['psk-flags'], '0')
            self.assertEqual(config['wifi-security']['psk'], r'\ssecret\s$\spass\s')

    def test_existing_secrets_clear_agent_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ident = str(uuid.uuid4())
            self.profile(root, ident, '[wifi-security]\nkey-mgmt=wpa-psk\npsk=existing\npsk-flags=1\n')
            profiles = network.read_wifi_profiles((root,), {ident}, secret_reader=lambda *args: '')
            self.assertIn('psk=existing', profiles[0].contents)
            self.assertIn('psk-flags=0', profiles[0].contents)

    def test_missing_active_secret_or_profile_stops_before_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ident = str(uuid.uuid4())
            self.profile(root, ident, '[wifi-security]\nkey-mgmt=sae\npsk-flags=1\n')
            with self.assertRaisesRegex(RuntimeError, 'Save the current Wi-Fi password'):
                network.read_wifi_profiles((root,), {ident}, secret_reader=lambda *args: '')
            with self.assertRaisesRegex(RuntimeError, 'no transferable profile'):
                network.read_wifi_profiles((), {ident}, secret_reader=lambda *args: '')

    def test_local_enterprise_certificates_are_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live, target = root / 'live', root / 'target'
            live.mkdir(); target.mkdir()
            certificate = root / 'certificate.pem'
            certificate.write_bytes(b'test certificate')
            ident = str(uuid.uuid4())
            self.profile(live, ident, '[wifi-security]\nkey-mgmt=wpa-eap\n[802-1x]\neap=tls;\nca-cert=' + str(certificate) + '\n')
            profiles = network.read_wifi_profiles((live,), {ident}, secret_reader=lambda *args: '')
            network.install_wifi_profiles(target, profiles)
            cert = target / 'etc/NetworkManager/certs' / (ident + '-ca-cert')
            self.assertEqual(cert.read_bytes(), certificate.read_bytes())
            self.assertEqual(cert.stat().st_mode & 0o777, 0o600)


class DesktopTests(unittest.TestCase):
    def test_selected_desktop_uses_chroot_environment_package(self):
        with patch.object(configure_fedora, 'target_resolver', return_value=nullcontext()), \
                patch.object(configure_fedora, 'run') as run:
            configure_fedora.install_desktop('kde')
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ['chroot', configure_fedora.TARGET, 'dnf5'])
            self.assertIn('@kde-desktop-environment', command)
            self.assertEqual(command[-1], 'plasma-login-manager')
            run.reset_mock()
            configure_fedora.install_desktop('none')
            run.assert_not_called()

    def test_console_and_desktop_installations_allow_graphical_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit = root / 'usr/lib/systemd/system/plasmalogin.service'
            unit.parent.mkdir(parents=True)
            unit.write_text('[Service]\n')
            with patch.object(configure_fedora, 'TARGET', root), patch.object(configure_fedora, 'run') as run:
                configure_fedora.configure_desktop('kde')
                self.assertIn('plasmalogin.service', run.call_args_list[0].args[0])
                self.assertEqual(run.call_args_list[-1].args[0][-1], 'graphical.target')
                configure_fedora.configure_desktop('none')
                self.assertEqual(run.call_args_list[-1].args[0][-1], 'graphical.target')

    def test_resolver_is_restored_even_after_failed_chroot_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'etc').mkdir()
            resolver = root / 'etc/resolv.conf'
            resolver.symlink_to('../run/systemd/resolve/stub-resolv.conf')
            with patch.object(configure_fedora, 'TARGET', root):
                with self.assertRaisesRegex(RuntimeError, 'test failure'):
                    with configure_fedora.target_resolver():
                        self.assertFalse(resolver.is_symlink())
                        self.assertTrue(resolver.read_text())
                        raise RuntimeError('test failure')
            self.assertEqual(resolver.readlink(), Path('../run/systemd/resolve/stub-resolv.conf'))


if __name__ == '__main__':
    unittest.main()
