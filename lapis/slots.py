"""Select and validate an A/B boot slot without changing its GPT attributes."""
from pathlib import Path
import re

from .common import require

BOOT_PARTITIONS = ('boot', 'init_boot', 'vendor_boot', 'vbmeta')


def boot_names(slot):
    require(slot in ('a', 'b'), 'Expected boot slot a or b')
    return tuple(name + '_' + slot for name in BOOT_PARTITIONS)


def active_slot(parts):
    """Return the uniquely preferred, bootable and successful GPT slot.

    A trial slot may be selected by firmware, but is not safe to overwrite with
    these in-place update tools. Do not silently fall back to the other slot.
    """
    candidates = []
    for slot in ('a', 'b'):
        require(all(name in parts for name in boot_names(slot)), 'Incomplete boot slot ' + slot.upper())
        attrs = parts['vbmeta_' + slot]['attributes']
        priority, tries, successful = (attrs >> 48 & 15, attrs >> 52 & 15, attrs >> 56 & 1)
        if priority and (successful or tries):
            candidates.append((priority, slot, successful))
    require(candidates, 'No bootable GPT slot')
    priority = max(p[0] for p in candidates)
    preferred = [p for p in candidates if p[0] == priority]
    require(len(preferred) == 1, 'Ambiguous GPT boot-slot priorities; refusing to choose a slot')
    _, slot, successful = preferred[0]
    require(successful, 'Preferred slot ' + slot.upper() + ' is not marked successful')
    return slot


def image_slot(records, declared=None):
    """Validate a complete, ordered boot set, including older A-only manifests."""
    names = [r['name'] for r in records]
    matches = [slot for slot in ('a', 'b') if names == list(boot_names(slot))]
    require(len(matches) == 1, 'Unexpected or mixed-slot boot image set')
    slot = matches[0]
    require(declared is None or declared == slot, 'Boot image names disagree with recorded slot')
    return slot


def reported_slot(cmdline, bootconfig):
    """Read firmware's slot report, if provided, from kernel boot parameters."""
    values = []
    pattern = r'(?:^|\s)androidboot\.slot(?:_suffix)?\s*=\s*(?:"([^"\n]*)"|(\S+))'
    for text in (cmdline, bootconfig):
        for quoted, bare in re.findall(pattern, text):
            value = quoted or bare
            require(value in ('a', 'b', '_a', '_b'), 'Invalid firmware boot-slot report')
            values.append(value.removeprefix('_'))
    require(len(set(values)) <= 1, 'Conflicting firmware boot-slot reports')
    return values[0] if values else None


def running_slot():
    config = Path('/proc/bootconfig')
    return reported_slot(Path('/proc/cmdline').read_text(), config.read_text() if config.is_file() else '')
