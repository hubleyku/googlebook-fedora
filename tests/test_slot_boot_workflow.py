"""Real AVB/loader checks on disposable sparse files; never open block devices."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from lapis.boot import build_set
from lapis.common import ROOT, read_json, save_json, sha256, runtime_sources
from lapis.cpio import archive
from lapis.gpt import ENTRY, read_gpt, tables
from lapis.slots import active_slot, boot_names
from tools import native_fedora, simulate_boot_commit as simulation
from tools.build_fedora_usb import create_usb_image, protective_mbr, validate_live_image
from lapis.installer_usb import usb_config
from tools.prepare_fedora_boot import check_profile, hash_region, make_cmdline
from tools.restore_fedora_boot import inspect_backup, validate_loader
from tools.restore_bootstrap import inspect_bootstrap, restore_bootstrap
from tools.install_fedora import save_installed_bootstrap


class SlotBootWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kernel = Path(os.environ.get('LAPIS_TEST_KERNEL',
                          '/usr/lib/modules/' + os.uname().release + '/vmlinuz'))
        cls.loader = ROOT / 'build/load_android_test'
        if not cls.kernel.is_file() or not cls.loader.is_file() or not shutil.which('openssl'):
            raise unittest.SkipTest('Set LAPIS_TEST_KERNEL to a Linux kernel and build the loader harness')

    def command(self, *args):
        result = subprocess.run([sys.executable, '-B', *map(str, args)],
                                capture_output=True, text=True,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def make_disk(self, path, slot):
        profile = read_json(ROOT / 'assets/lapis-512gb.json')
        size = profile['disk_bytes']
        entries = bytearray(128 * 128)
        for part in profile['partitions']:
            name = part['name']
            attrs = 0
            if name.startswith('vbmeta_'):
                attrs = (15 if name.endswith('_' + slot) else 14) << 48 | 1 << 56
            if name.startswith('pvmfw_'):
                name = 'factory_' + name
            start = part['start_lba']
            offset = (part['index'] - 1) * 128
            entries[offset:offset + 128] = ENTRY.pack(
                uuid.UUID(part['type_guid']).bytes_le, uuid.uuid4().bytes_le,
                start, start + part['size'] // 512 - 1, attrs, name.encode('utf-16-le'))
        last = size // 512 - 1
        header = [b'EFI PART', 0x10000, 92, 0, 0, 1, last, 34, last - 33,
                  uuid.uuid4().bytes_le, 2, 128, 128, 0]
        primary, secondary = tables(header, entries, protective_mbr(last))
        with path.open('xb') as disk:
            disk.truncate(size)
            disk.write(primary)
            disk.seek(size - len(secondary)); disk.write(secondary)
        with path.open('rb') as disk:
            gpt = read_gpt(disk, size)
            self.assertEqual(active_slot(check_profile(gpt)), slot)
        return gpt

    def test_both_slots_prepare_commit_rollback_and_restore(self):
        for slot in ('a', 'b'):
            with self.subTest(slot=slot), tempfile.TemporaryDirectory(prefix='lapis-slots-') as tmp:
                self.exercise_slot(Path(tmp), slot)

    def exercise_slot(self, tmp, slot):
        disk_path = tmp / 'disk.img'
        gpt = self.make_disk(disk_path, slot)
        parts = {p['name']: p for p in gpt.partitions()}
        names = boot_names(slot)
        inactive = boot_names('b' if slot == 'a' else 'a')
        old_initrd, new_initrd = tmp / 'old.cpio', tmp / 'new.cpio'
        old_initrd.write_bytes(archive({'old': (0o100644, b'old ramdisk')}))
        new_initrd.write_bytes(archive({'new': (0o100644, b'new ramdisk')}))
        old = tmp / 'old'
        root_uuid = str(uuid.uuid4())
        # Real signing and AVB verification, using a real bzImage. The ramdisk
        # markers exercise image integrity, not Linux userspace bootability.
        build_set(self.kernel, old_initrd, make_cmdline(root_uuid), old, runtime_sources(),
                  sizes={name: parts[name]['size'] for name in names}, installed=True,
                  kernel_sha256=sha256(self.kernel), slot=slot)
        # Compact USB construction and loader validation need no filesystem
        # formatter; the writable partition is created later by the writer.
        iso = tmp / 'live.iso'
        iso.write_bytes(bytes(1048576))
        usb_work = tmp / 'usb-work'
        usb_work.mkdir()
        usb = tmp / 'installer.img'
        create_usb_image(usb, iso, read_json(old / 'manifest.json'), old, usb_work,
                         usb_config(), 64 * 1024**2)
        validate_live_image(usb, usb_work, self.loader)
        with disk_path.open('r+b') as disk:
            for name in names:
                disk.seek(parts[name]['start_lba'] * 512)
                disk.write((old / (name + '.img')).read_bytes())
            for name in inactive:
                disk.seek(parts[name]['start_lba'] * 512)
                disk.write(('untouched ' + name).encode())
        with disk_path.open('rb') as disk:
            inactive_hashes = {name: hash_region(disk, parts[name]['start_lba'] * 512, parts[name]['size'])
                               for name in inactive}
        stage = tmp / 'stage'
        self.command(ROOT / 'tools/prepare_fedora_boot.py', '--disk', disk_path,
                     '--kernel', self.kernel, '--initrd', new_initrd, '--root-uuid', root_uuid,
                     '--output', stage)
        proof = read_json(stage / 'validation.json')
        self.assertEqual(proof['slot'], slot)
        self.assertEqual([r['name'] for r in proof['source_regions']], list(names))
        self.assertIn('vb2api_load_kernel() returned 0', (stage / 'loader.log').read_text())
        if slot == 'a':
            # Legacy prepared boot sets also lacked explicit slot fields.
            manifest_path = stage / 'candidate/manifest.json'
            manifest = read_json(manifest_path)
            del manifest['slot']
            save_json(manifest_path, manifest)
            del proof['slot']
            proof['candidate_manifest_sha256'] = sha256(manifest_path)
            save_json(stage / 'validation.json', proof)
        with disk_path.open('rb') as disk:
            checked, candidate = simulation.inspect(disk, stage)
            other = 'b' if slot == 'a' else 'a'
            wrong = dict(candidate, slot=other, images=[{'name': name} for name in boot_names(other)])
            rejected_backup = tmp / 'rejected-backup'
            with self.assertRaisesRegex(RuntimeError, 'Candidate and backup slots differ'):
                simulation.commit(disk, stage, rejected_backup, checked, wrong)
            self.assertFalse(rejected_backup.exists())
        backup = tmp / 'backup'
        self.command(ROOT / 'tools/simulate_boot_commit.py', '--disk-image', disk_path,
                     '--stage', stage, '--backup', backup, '--apply')
        self.assertEqual(read_json(backup / 'manifest.json')['slot'], slot)
        bootstrap = tmp / 'linux-bootstrap'
        save_json(stage / 'plan.json', dict(device=str(disk_path), disk_guid=gpt.guid,
                  disk_bytes=gpt.disk_bytes, partitions=gpt.partitions(), slot=slot))
        with patch.object(native_fedora, 'STATE', stage):
            save_installed_bootstrap(disk_path, tmp)
        self.assertEqual({p.name for p in bootstrap.iterdir()},
                         {name + '.img' for name in names} | {'misc.img', 'gpt-primary.bin', 'gpt-secondary.bin', 'plan.json', 'manifest.json'})
        restored_layout, boot_regions = inspect_bootstrap(bootstrap)
        validate_loader(restored_layout, boot_regions, self.loader)
        blank = tmp / 'blank.img'
        with blank.open('x+b', buffering=0) as disk:
            disk.truncate(gpt.disk_bytes)
            root_offset = parts['userdata']['start_lba'] * 512
            disk.seek(root_offset); disk.write(b'untouched root filesystem')
            restore_bootstrap(disk, restored_layout, boot_regions)
            self.assertEqual(read_gpt(disk, gpt.disk_bytes).partitions(), gpt.partitions())
            disk.seek(root_offset)
            self.assertEqual(disk.read(25), b'untouched root filesystem')
            for name, offset, data in boot_regions:
                self.assertEqual(hash_region(disk, offset, len(data)), sha256(bootstrap / (name + '.img')))
        # A partial or corrupt bootstrap must fail before any target is opened.
        with (bootstrap / ('boot_' + slot + '.img')).open('r+b') as image:
            image.write(b'corrupted')
        with self.assertRaisesRegex(RuntimeError, 'hash failed'):
            inspect_bootstrap(bootstrap)
        with disk_path.open('rb') as disk:
            for record in read_json(stage / 'candidate/manifest.json')['images']:
                self.assertEqual(hash_region(disk, parts[record['name']]['start_lba'] * 512, record['size']), record['sha256'])
            restored_gpt, regions = inspect_backup(disk, gpt.disk_bytes, backup)
        validate_loader(restored_gpt, regions, self.loader)
        # Simulate the legacy A-only backup format, which has no slot field.
        if slot == 'a':
            legacy = read_json(backup / 'manifest.json')
            del legacy['slot']
            save_json(backup / 'manifest.json', legacy)
        self.command(ROOT / 'tools/restore_fedora_boot.py', '--disk-image', disk_path,
                     '--backup', backup, '--apply')
        self.assert_disk_unchanged(disk_path, gpt, proof, parts, inactive_hashes)

        # Installer candidates must also use the detected slot. A mixed or
        # wrong-slot candidate must fail before any disk write is attempted.
        (old / 'misc.img').write_bytes(bytes(parts['misc']['size']))
        initial_plan = dict(disk_guid=gpt.guid, disk_bytes=gpt.disk_bytes, partitions=gpt.partitions())
        if slot == 'b':
            initial_plan['slot'] = slot
        save_json(old / 'plan.json', initial_plan)
        records = [dict(file=name + '.img', size=(old / (name + '.img')).stat().st_size,
                        sha256=sha256(old / (name + '.img'))) for name in names]
        records.append(dict(file='plan.json', size=(old / 'plan.json').stat().st_size,
                            sha256=sha256(old / 'plan.json')))
        save_json(old / 'manifest.json', dict(disk_guid=gpt.guid, records=records))
        with disk_path.open('rb') as disk:
            _, restored = inspect_backup(disk, gpt.disk_bytes, old)
            self.assertEqual([name for name, _, _ in restored], list(names))
        with patch.object(native_fedora, 'STATE', stage):
            plan = dict(partitions=gpt.partitions(), disk_bytes=gpt.disk_bytes, slot=slot)
            regions = native_fedora.candidate_regions(plan, gpt, old)
            self.assertEqual([offset for offset, _, _ in regions[:4]],
                             [parts[name]['start_lba'] * 512 for name in names])
            plan['slot'] = 'b' if slot == 'a' else 'a'
            with self.assertRaisesRegex(RuntimeError, 'Planned boot slot differs'):
                native_fedora.candidate_regions(plan, gpt, old)

        # Interrupt an actual regular-file write and exercise the production
        # rollback path, including hashes of restored and inactive regions.
        real_write = simulation.write_file
        calls = 0

        def interrupted_write(fd, offset, path):
            nonlocal calls
            calls += 1
            real_write(fd, offset, path)
            if calls == 2:
                raise OSError('simulated interrupted write')

        failed_backup = tmp / 'failed-backup'
        with disk_path.open('r+b', buffering=0) as disk:
            checked, manifest = simulation.inspect(disk, stage)
            with patch.object(simulation, 'write_file', interrupted_write):
                with self.assertRaisesRegex(OSError, 'simulated interrupted write'):
                    simulation.commit(disk, stage, failed_backup, checked, manifest)
        self.assertTrue(all(r['restored'] for r in read_json(failed_backup / 'rollback.json')))
        self.assert_disk_unchanged(disk_path, gpt, proof, parts, inactive_hashes)

        # Switching the preferred slot after staging must invalidate both an
        # update and a restore, even though the partition geometry is unchanged.
        self.switch_slot(disk_path, gpt, 'b' if slot == 'a' else 'a')
        with disk_path.open('rb') as disk:
            with self.assertRaises(RuntimeError):
                simulation.inspect(disk, stage)
            with self.assertRaises(RuntimeError):
                inspect_backup(disk, gpt.disk_bytes, backup)

    def assert_disk_unchanged(self, path, gpt, proof, parts, inactive_hashes):
        with path.open('rb') as disk:
            current = read_gpt(disk, gpt.disk_bytes)
            self.assertEqual((current.primary, current.secondary), (gpt.primary, gpt.secondary))
            for record in proof['source_regions']:
                self.assertEqual(hash_region(disk, record['offset'], record['size']), record['sha256'])
            for name, digest in inactive_hashes.items():
                self.assertEqual(hash_region(disk, parts[name]['start_lba'] * 512, parts[name]['size']), digest)

    def switch_slot(self, path, gpt, slot):
        entries = bytearray(gpt.entries)
        for part in gpt.partitions():
            if part['name'].startswith('vbmeta_'):
                offset = (part['index'] - 1) * 128
                values = list(ENTRY.unpack(entries[offset:offset + 128]))
                values[4] = (15 if part['name'].endswith('_' + slot) else 14) << 48 | 1 << 56
                entries[offset:offset + 128] = ENTRY.pack(*values)
        primary, secondary = tables(gpt.header, entries, gpt.primary[:512])
        with path.open('r+b') as disk:
            disk.write(primary)
            disk.seek(gpt.disk_bytes - len(secondary)); disk.write(secondary)


if __name__ == '__main__':
    unittest.main()
