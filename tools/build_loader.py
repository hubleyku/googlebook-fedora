#!/usr/bin/env python3
"""Build the read-only ChromiumOS Android loader harness on Linux or macOS."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, require, run


def build_loader(vendor, destination, runner=run):
    cc = os.environ.get('CC', 'cc')
    missing = [name for name in ('make', cc) if not shutil.which(name)]
    require(not missing, 'Missing build tools: ' + ', '.join(missing) +
            '. On Fedora, install them with: sudo dnf install gcc make')
    vendor = Path(vendor).resolve()
    vboot = vendor / 'vboot'
    require((vboot / 'Makefile').is_file() and (vendor / 'avb/libavb/avb_version.h').is_file(),
            'Fetch pinned sources first: python3 -B tools/fetch_sources.py')
    dest = Path(destination).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    compat = dest.parent / 'compat'
    compat.mkdir(exist_ok=True)
    if platform.system() == 'Darwin':
        pairs = [('htobe', 'OSSwapHostToBigInt'), ('htole', 'OSSwapHostToLittleInt'),
                 ('be', 'OSSwapBigToHostInt'), ('le', 'OSSwapLittleToHostInt')]
        text = '#pragma once\n#include <libkern/OSByteOrder.h>\n'
        for prefix, fn in pairs:
            for bits in (16, 32, 64):
                name = prefix + str(bits) + ('toh' if prefix in ('be', 'le') else '')
                text += f'#define {name} {fn}{bits}\n'
        (compat / 'endian.h').write_text(text)
    runner(['make', '-C', vboot, '-j2', 'FIRMWARE_ARCH=mock', 'USE_AVB=1',
         'LIBAVB_SRCDIR=' + str(vendor / 'avb') + '/', 'CC=' + cc,
         'TEST_FLAGS=-O2 -I' + str(compat), 'fwlib'])
    includes = ['firmware/2lib/include', 'firmware/include', 'firmware/lib/include',
                'firmware/lib/cgptlib/include', 'host/lib/include', 'host/lib21/include', 'host/include']
    runner([cc, '-O2', '-DUSE_AVB=1', '-I' + str(compat),
         *('-I' + str(vboot / p) for p in includes), ROOT / 'tools/loader_harness.c',
         vboot / 'build/vboot_fw.a', '-o', dest])
    print('Built', dest)
    return dest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor', type=Path, default=ROOT / 'build/sources')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/load_android_test')
    args = parser.parse_args()
    build_loader(args.vendor, args.output)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nLoader build cancelled.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print('Loader build stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
