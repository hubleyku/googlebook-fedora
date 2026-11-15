"""Exercise updater staging and commit ordering without opening a disk."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lapis.common import read_json, save_json, sha256
from tools import auto_fedora_update as automatic, prepare_fedora_update as preparation


class KernelUpdateTests(unittest.TestCase):
    def stage_dependencies(self, stack, root, slot):
        kernel = root / 'vmlinuz'
        kernel.write_bytes(b'test candidate kernel')
        system = ('test-root-uuid', '7.2.11-300.fc45.x86_64', kernel)
        stack.enter_context(patch.object(preparation.shutil, 'which', return_value='/test/tool'))
        stack.enter_context(patch.object(preparation, 'verify_system', return_value=system))

        def build(command):
            output = Path(command[command.index('--output') + 1])
            if str(command[1]).endswith('build_fedora_initramfs.py'):
                output.write_bytes(b'test initramfs')
            elif str(command[1]).endswith('prepare_fedora_boot.py'):
                output.mkdir()
                save_json(output / 'validation.json', {'slot': slot, 'loader_accepted': True})
            else:
                self.fail('Unexpected provisioning command: ' + repr(command))

        stack.enter_context(patch.object(preparation, 'run', side_effect=build))
        return system

    def test_preparation_records_validated_slot_and_artifact_hashes(self):
        for slot in ('a', 'b'):
            with self.subTest(slot=slot), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                root_uuid, running, kernel = self.stage_dependencies(stack, root, slot)
                destination = root / 'candidate'
                preparation.prepare(Path('/dev/test-disk'), '7.3.3-300.fc45.x86_64', destination)
                self.assertEqual(read_json(destination / 'update.json'), {
                    'running_kernel': running, 'candidate_kernel': '7.3.3-300.fc45.x86_64',
                    'slot': slot, 'root_uuid': root_uuid, 'kernel_sha256': sha256(kernel),
                    'initrd_sha256': sha256(destination / 'initramfs.img'),
                    'validation_sha256': sha256(destination / 'bootset/validation.json'),
                    'status': 'prepared only; not installed',
                })

    def automatic_dependencies(self, stack, root, slot):
        system = self.stage_dependencies(stack, root, slot)
        state, backups = root / 'updates', root / 'backups'
        stack.enter_context(patch.object(automatic, 'STATE', state))
        stack.enter_context(patch.object(automatic, 'LOCAL_BACKUPS', backups))
        stack.enter_context(patch.object(automatic.os, 'geteuid', return_value=0))
        stack.enter_context(patch.object(automatic, 'private_directory',
                                        side_effect=lambda path: path.mkdir(mode=0o700)))
        stack.enter_context(patch.object(automatic, 'device_for_root', return_value=Path('/dev/test-disk')))
        stack.enter_context(patch.object(automatic, 'boot_identity', return_value=('test-disk-guid', slot, sha256(system[2]))))
        stack.enter_context(patch.object(automatic, 'output', return_value=system[0]))
        stack.enter_context(patch.object(automatic, 'verify_system', return_value=system))
        stack.enter_context(patch.object(automatic, 'installed_boot_version', return_value=system[1]))
        stack.enter_context(patch.object(automatic, 'version_compare', return_value=1))
        return state

    def test_automatic_update_commits_only_after_complete_preparation(self):
        for slot in ('a', 'b'):
            with self.subTest(slot=slot), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                state = self.automatic_dependencies(stack, Path(tmp), slot)

                def commit(device, stage, backup, **kwargs):
                    update = read_json(stage / 'update.json')
                    self.assertEqual(update['slot'], slot)
                    self.assertEqual(update['candidate_kernel'], '7.3.3-300.fc45.x86_64')
                    self.assertEqual(update['validation_sha256'], sha256(stage / 'bootset/validation.json'))
                    self.assertEqual(update['status'], 'prepared only; not installed')
                    self.assertTrue(kwargs['automatic'])
                    self.assertEqual(read_json(state / 'status.json')['status'], 'preparing')

                commit_mock = stack.enter_context(patch.object(automatic, 'commit_prepared', side_effect=commit))
                automatic.update_kernel('7.3.3-300.fc45.x86_64')
                commit_mock.assert_called_once()
                pending = read_json(state / 'pending.json')
                self.assertEqual(pending['kernel_version'], '7.3.3-300.fc45.x86_64')
                self.assertEqual(pending['slot'], slot)
                self.assertEqual(read_json(state / 'status.json')['status'], 'updated')
                self.assertFalse(list(state.glob('stage-*')))

    def test_preparation_failure_never_calls_commit_or_records_pending_kernel(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            state = self.automatic_dependencies(stack, Path(tmp), 'b')
            stack.enter_context(patch.object(automatic, 'prepare', side_effect=RuntimeError('preparation failed')))
            commit = stack.enter_context(patch.object(automatic, 'commit_prepared'))
            with self.assertRaisesRegex(RuntimeError, 'preparation failed'):
                automatic.update_kernel('7.3.3-300.fc45.x86_64')
            commit.assert_not_called()
            self.assertFalse((state / 'pending.json').exists())
            self.assertFalse(list(state.glob('stage-*')))


if __name__ == '__main__':
    unittest.main()
