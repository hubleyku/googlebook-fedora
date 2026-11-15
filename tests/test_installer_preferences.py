from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from lapis.common import save_json
from lapis.installation import InstallApproval, Preferences
from lapis.slots import boot_names
from tools import install_fedora as installer


class PreferencesTests(unittest.TestCase):
    def test_invalid_values_retry_only_the_current_question(self):
        answers = iter(['Not/A_Zone', 'UTC', 'bad locale', 'fr_CA', 'not-a-keymap', 'us', 'root', 'sddm',
                        'installtest', '99', 'KDE Plasma'])
        secrets = iter(['', 'first', 'different', 'Good password $ café', 'Good password $ café'])
        prompts = []
        def answer(prompt):
            prompts.append(prompt)
            return next(answers)
        stream = io.StringIO()
        with redirect_stdout(stream):
            result = installer.collect_preferences(keymaps={'us'}, input_fn=answer,
                                                   password_fn=lambda prompt: next(secrets))
        self.assertEqual(result.public(), dict(timezone='UTC', keymap='us', user='installtest', desktop='kde', hostname='googlebook', locale='fr_CA.UTF-8'))
        self.assertEqual(sum(p.startswith('Timezone') for p in prompts), 2)
        self.assertEqual(sum(p.startswith('Locale') for p in prompts), 2)
        self.assertEqual(sum(p.startswith('Username') for p in prompts), 3)
        self.assertIn('Passwords do not match', stream.getvalue())
        self.assertNotIn(result.password, stream.getvalue())
        self.assertNotIn(result.password, repr(result))

    def test_confirmation_accepts_only_uppercase_yes_and_prints_no_password(self):
        answers = iter(['yes', 'y', 'YES'])
        stream = io.StringIO()
        prefs = Preferences('UTC', 'us', 'installtest', 'Private Password')
        with redirect_stdout(stream):
            installer.confirm(prefs, Path('/dev/nvme0n1'), 'b', Path('/run/lapis-usb'), 1,
                              input_fn=lambda prompt: next(answers))
        self.assertIn('\033[1;31m', stream.getvalue())
        self.assertIn('cannot be undone', stream.getvalue())
        self.assertNotIn(prefs.password, stream.getvalue())

    def test_end_of_input_cancels_without_any_installation_work(self):
        with patch.object(installer.os, 'geteuid', return_value=0), \
                patch.object(installer.sys.stdin, 'isatty', return_value=True), \
                patch.object(installer, 'collect_preferences', side_effect=EOFError), \
                patch.object(installer, 'ensure_mounted') as mount, \
                patch.object(installer, 'perform_install') as install:
            self.assertEqual(installer.main([]), 130)
            mount.assert_not_called()
            install.assert_not_called()

    def test_all_preferences_precede_preflight_confirmation_and_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            prefs = Preferences('UTC', 'us', 'installtest', 'Private Password')
            parts = [dict(name=name, attributes=(15 << 48 | 1 << 56) if name == 'vbmeta_b' else 0)
                     for slot in ('a', 'b') for name in boot_names(slot)]
            gpt = SimpleNamespace(guid='disk', primary=b'primary', secondary=b'secondary', partitions=lambda: parts)
            with ExitStack() as stack:
                stack.enter_context(patch.object(installer.os, 'geteuid', return_value=0))
                stack.enter_context(patch.object(installer.sys.stdin, 'isatty', return_value=True))
                stack.enter_context(patch.object(installer.native_fedora, 'STATE', Path(tmp) / 'state'))
                stack.enter_context(patch.object(installer, 'collect_preferences', side_effect=lambda: events.append('preferences') or prefs))
                stack.enter_context(patch.object(installer, 'find_target', side_effect=lambda explicit: events.append('target') or (Path('/dev/nvme0n1'), gpt)))
                stack.enter_context(patch.object(installer, 'ensure_mounted', side_effect=lambda: events.append('mount') or Path(tmp)))
                stack.enter_context(patch.object(installer, 'required_backup_free_bytes', return_value=1024))
                stack.enter_context(patch.object(installer.native_fedora, 'external', side_effect=lambda *args: events.append('backup-location')))
                stack.enter_context(patch.object(installer, 'read_wifi_profiles', side_effect=lambda: events.append('wifi') or []))
                stack.enter_context(patch.object(installer, 'verify_package_sources', side_effect=lambda *args: events.append('packages')))
                stack.enter_context(patch.object(installer, 'confirm', side_effect=lambda *args: events.append('YES')))
                stack.enter_context(patch.object(installer, 'perform_install', side_effect=lambda *args: events.append('install')))
                self.assertEqual(installer.main([]), 0)
            self.assertEqual(events, ['preferences', 'target', 'mount', 'backup-location', 'wifi', 'packages', 'YES', 'install'])


class InstallationOrderingTests(unittest.TestCase):
    def test_approval_is_bound_to_disk_and_partition_table(self):
        plan = dict(device='/dev/nvme0n1', disk_guid='disk', gpt_sha256='table')
        approval = InstallApproval.for_plan(plan)
        approval.check(plan)
        for key in plan:
            changed = dict(plan, **{key: 'different'})
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                approval.check(changed)

    def exercise(self, failed_backup=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / 'state'
            plan = dict(device='/dev/nvme0n1', disk_guid='abcd-disk', gpt_sha256='table')
            approval = InstallApproval.for_plan(plan)
            prefs = Preferences('UTC', 'us', 'installtest', 'Private Password', 'xfce')
            events = []
            def prepare(args):
                events.append('plan')
                state.mkdir()
                save_json(state / 'plan.json', plan)
            def backup(args):
                events.append('backup')
                args.destination.mkdir()
                if failed_backup:
                    raise RuntimeError('backup verification failed')
            def format_disk(args, *, approval):
                events.append('format')
                approval.check(plan)
                (state / 'format-started').touch()
            def commit(args, *, approval):
                approval.check(plan)
                events.append('commit')
            with ExitStack() as stack:
                stack.enter_context(patch.object(installer.native_fedora, 'STATE', state))
                stack.enter_context(patch.object(installer.native_fedora, 'plan', side_effect=prepare))
                stack.enter_context(patch.object(installer.native_fedora, 'backup', side_effect=backup))
                stack.enter_context(patch.object(installer.native_fedora, 'format_root', side_effect=format_disk))
                packages = stack.enter_context(patch.object(installer.configure_fedora, 'packages', side_effect=lambda *a, **k: events.append('packages')))
                setup = stack.enter_context(patch.object(installer.configure_fedora, 'setup', side_effect=lambda *a, **k: events.append('setup')))
                stack.enter_context(patch.object(installer.native_fedora, 'build', side_effect=lambda *a: events.append('build')))
                stack.enter_context(patch.object(installer.native_fedora, 'validate', side_effect=lambda *a: events.append('validate')))
                stack.enter_context(patch.object(installer.native_fedora, 'commit', side_effect=commit))
                stack.enter_context(patch.object(installer, 'save_installed_bootstrap', side_effect=lambda *a: events.append('bootstrap')))
                stack.enter_context(patch.object(installer, 'archive_state'))
                stack.enter_context(patch.object(installer, 'cleanup_target', side_effect=lambda *a: events.append('cleanup')))
                # Any new prompt in the confirmed workflow is a regression.
                stack.enter_context(patch('builtins.input', side_effect=AssertionError('unexpected prompt')))
                if failed_backup:
                    with self.assertRaisesRegex(RuntimeError, 'backup verification failed'):
                        installer.perform_install(prefs, Path(plan['device']), root, [], approval)
                    self.assertEqual(events, ['plan', 'backup'])
                    packages.assert_not_called()
                else:
                    installer.perform_install(prefs, Path(plan['device']), root, [], approval)
                    self.assertEqual(events, ['plan', 'backup', 'format', 'packages', 'setup', 'build', 'validate', 'commit', 'bootstrap', 'cleanup'])
                    self.assertEqual(packages.call_args.kwargs['password'], prefs.password)
                    self.assertEqual(packages.call_args.args[0].locale, 'en_US.UTF-8')
                    self.assertEqual(setup.call_args.kwargs['wifi_profiles'], [])

    def test_confirmed_workflow_has_no_more_prompts(self):
        self.exercise()

    def test_backup_failure_prevents_formatting(self):
        self.exercise(failed_backup=True)


if __name__ == '__main__':
    unittest.main()
