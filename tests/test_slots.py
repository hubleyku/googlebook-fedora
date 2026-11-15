import unittest
from unittest.mock import patch

from lapis.slots import active_slot, boot_names, image_slot, reported_slot
from tools.auto_fedora_update import pending_expectation


def slot_parts(a, b):
    return {name: {'attributes': attrs if name.startswith('vbmeta_') else 0}
            for slot, attrs in (('a', a), ('b', b)) for name in boot_names(slot)}


def attributes(priority, successful=True, tries=0):
    return priority << 48 | tries << 52 | int(successful) << 56


class SlotSelectionTests(unittest.TestCase):
    def test_either_slot_can_be_preferred(self):
        for slot in ('a', 'b'):
            with self.subTest(slot=slot):
                parts = slot_parts(attributes(15 if slot == 'a' else 14),
                                   attributes(15 if slot == 'b' else 14))
                self.assertEqual(active_slot(parts), slot)

    def test_unbootable_high_priority_slot_is_ignored(self):
        parts = slot_parts(attributes(15, False), attributes(14))
        self.assertEqual(active_slot(parts), 'b')

    def test_ambiguous_unbootable_and_trial_states_are_refused(self):
        cases = [slot_parts(attributes(15), attributes(15)),
                 slot_parts(attributes(0), attributes(0)),
                 slot_parts(attributes(14), attributes(15, False, tries=3))]
        for parts in cases:
            with self.subTest(parts=parts), self.assertRaises(RuntimeError):
                active_slot(parts)

    def test_incomplete_slot_is_refused(self):
        parts = slot_parts(attributes(15), attributes(14))
        del parts['vendor_boot_b']
        with self.assertRaises(RuntimeError):
            active_slot(parts)

    def test_legacy_and_explicit_image_records(self):
        for slot in ('a', 'b'):
            records = [{'name': name} for name in boot_names(slot)]
            self.assertEqual(image_slot(records), slot)
            self.assertEqual(image_slot(records, slot), slot)
            with self.assertRaises(RuntimeError):
                image_slot(records, 'b' if slot == 'a' else 'a')
        records[0]['name'] = 'boot_a'
        with self.assertRaises(RuntimeError):
            image_slot(records)

    def test_firmware_slot_reports(self):
        self.assertEqual(reported_slot('androidboot.slot_suffix=_a', ''), 'a')
        self.assertEqual(reported_slot('', 'androidboot.slot_suffix = "_b"\n'), 'b')
        self.assertEqual(reported_slot('androidboot.slot=b', 'androidboot.slot_suffix = "_b"'), 'b')
        self.assertIsNone(reported_slot('root=UUID=example', ''))
        for cmdline, config in [('androidboot.slot_suffix=_a', 'androidboot.slot_suffix = "_b"'),
                                ('', 'androidboot.slot_suffix = "_c"')]:
            with self.subTest(cmdline=cmdline, config=config), self.assertRaises(RuntimeError):
                reported_slot(cmdline, config)

    def test_pending_record_is_bound_to_its_slot(self):
        record = dict(disk_guid='disk', root_uuid='root', kernel_sha256='hash')
        # Old records were A-only. Even an identical kernel hash in B cannot
        # authorize using an A pending record after the selected slot changes.
        with patch('tools.auto_fedora_update.boot_identity', return_value=('disk', 'b', 'hash')):
            self.assertIsNone(pending_expectation(None, record, 'root'))
            record['slot'] = 'a'
            self.assertIsNone(pending_expectation(None, record, 'root'))

    def test_pending_record_accepts_b_and_legacy_a(self):
        for slot in ('a', 'b'):
            record = dict(disk_guid='disk', root_uuid='root', kernel_sha256='hash',
                          kernel_version='7.2.11-300.fc45.x86_64')
            if slot == 'b':
                record['slot'] = slot
            with self.subTest(slot=slot), \
                    patch('tools.auto_fedora_update.boot_identity', return_value=('disk', slot, 'hash')), \
                    patch('tools.auto_fedora_update.sha256', return_value='hash'), \
                    patch('pathlib.Path.is_file', return_value=True), \
                    patch('pathlib.Path.is_symlink', return_value=False):
                self.assertEqual(pending_expectation(None, record, 'root'), 'hash')


if __name__ == '__main__':
    unittest.main()
