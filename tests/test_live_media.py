from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lapis.live import validate_live_environment, workstation_label
from tools import build_fedora_usb as builder


class LiveMediaTests(unittest.TestCase):
    def test_live_environment_accepts_different_workstation_releases(self):
        for version in ('44', '45', '46', 'Rawhide'):
            with self.subTest(version=version):
                release = {'ID': 'fedora', 'VARIANT_ID': 'workstation', 'VERSION_ID': version}
                validate_live_environment(release, 'root=live:CDLABEL=Fedora-WS-Live-' + version +
                                           ' rd.live.image console=tty0', 'x86_64')

    def test_live_environment_rejects_other_editions_architectures_and_installed_systems(self):
        workstation = {'ID': 'fedora', 'VARIANT_ID': 'workstation'}
        live = 'root=live:CDLABEL=Fedora-WS-Live-46 rd.live.image'
        cases = [({'ID': 'fedora', 'VARIANT_ID': 'server'}, live, 'x86_64'),
                 ({'ID': 'ubuntu', 'VARIANT_ID': 'workstation'}, live, 'x86_64'),
                 (workstation, live, 'aarch64'), (workstation, 'root=UUID=test rw', 'x86_64'),
                 (workstation, 'root=live:CDLABEL=Fedora-KDE-Live-46 rd.live.image', 'x86_64')]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                validate_live_environment(*args)
        for label in ('Fedora-S-dvd-x86_64-45', 'Fedora-KDE-Live-46', 'Fedora-WS-Live-', 'Other-Live-46'):
            with self.subTest(label=label), self.assertRaises(RuntimeError):
                workstation_label(label)

    def test_boot_members_support_both_layouts_and_reject_incomplete_images(self):
        layouts = [('boot/x86_64/loader/linux', 'boot/x86_64/loader/initrd'),
                   ('images/pxeboot/vmlinuz', 'images/pxeboot/initrd.img')]
        for kernel, initrd in layouts:
            with self.subTest(kernel=kernel):
                listing = subprocess.CompletedProcess([], 0, './' + kernel + '\n' + initrd + '\n')
                with patch.object(builder.subprocess, 'run', return_value=listing):
                    self.assertEqual(builder.live_boot_members(Path('fedora.iso')),
                                     (('vmlinuz', './' + kernel), ('initrd.img', initrd)))
        with patch.object(builder.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, layouts[0][0])):
            with self.assertRaisesRegex(RuntimeError, 'no supported'):
                builder.live_boot_members(Path('fedora.iso'))

    def test_kernel_must_be_x86_64(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'kernel'
            header = bytearray(0x238)
            header[0x202:0x206] = b'HdrS'
            struct.pack_into('<H', header, 0x206, 0x20c)
            struct.pack_into('<H', header, 0x236, 1)
            path.write_bytes(header)
            builder.validate_live_kernel(path)
            for bad in (b'not a kernel', header[:0x236] + bytes(2)):
                path.write_bytes(bad)
                with self.assertRaisesRegex(RuntimeError, 'x86-64'):
                    builder.validate_live_kernel(path)


if __name__ == '__main__':
    unittest.main()
