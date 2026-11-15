import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from lapis.common import ROOT
from tools import configure_fedora
from tools.build_fedora_usb import vendor_payload


class RecoveryKitTests(unittest.TestCase):
    def setUp(self):
        # Packaging and --help checks need bytes to copy, not a compiled loader.
        self.workspace = tempfile.TemporaryDirectory(prefix='lapis-kit-source-')
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        for folder in ('lapis', 'tools', 'assets', 'docs'):
            shutil.copytree(ROOT / folder, self.root / folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('README.md', 'LICENSE', 'NOTICE.md', 'CONTRIBUTING.md'):
            shutil.copy2(ROOT / name, self.root / name)
        for relative, data in vendor_payload(ROOT / 'build/sources', ROOT).items():
            path = self.root / 'vendor' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root / 'build').mkdir()
        (self.root / 'build/load_android_test').write_bytes(b'loader packaging fixture')

    def test_standalone_kit_has_slot_helper_and_runs_outside_checkout(self):
        with tempfile.TemporaryDirectory(prefix='lapis-recovery-test-') as tmp:
            path = Path(tmp)
            archive = path / 'kit.tar.gz'
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-B', str(self.root / 'tools/build_recovery_kit.py'),
                                     '--output', str(archive)], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with tarfile.open(archive) as bundle:
                self.assertIn('lapis-recovery-kit/lapis/slots.py', bundle.getnames())
                self.assertIn('lapis-recovery-kit/docs/recovery.md', bundle.getnames())
                for name in ('tools/LICENSE.chromium', 'tools/LICENSE.apache-2.0', 'vendor/avb/LICENSE'):
                    self.assertIn('lapis-recovery-kit/' + name, bundle.getnames())
                self.assertFalse(any(name.endswith(('.pem', '.img')) for name in bundle.getnames()))
                bundle.extractall(path=path, filter='data')
            for script in ('restore_fedora_boot.py', 'restore_bootstrap.py'):
                result = subprocess.run([sys.executable, '-B', str(path / 'lapis-recovery-kit/tools' / script),
                                         '--help'], cwd=path, capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('--disk-image', result.stdout)

    def test_installed_kit_includes_recovery_guide_and_slot_helper(self):
        with tempfile.TemporaryDirectory(prefix='lapis-install-kit-test-') as tmp:
            with patch.object(configure_fedora, 'TARGET', Path(tmp)), \
                    patch.object(configure_fedora, 'ROOT', self.root):
                kit = configure_fedora.install_kit()
            self.assertEqual((kit / 'lapis/slots.py').read_bytes(), (ROOT / 'lapis/slots.py').read_bytes())
            self.assertTrue((kit / 'docs/recovery.md').is_file())
            for name in ('tools/LICENSE.chromium', 'tools/LICENSE.apache-2.0', 'vendor/avb/LICENSE'):
                self.assertTrue((kit / name).is_file())


if __name__ == '__main__':
    unittest.main()
