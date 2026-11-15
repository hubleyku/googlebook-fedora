#!/usr/bin/env python3
"""Bundle the standalone live-USB boot recovery tools without build secrets."""
import argparse
import io
from pathlib import Path
import sys
import tarfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, new_regular_output, require_regular


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/lapis-recovery-kit.tar.gz')
    args = parser.parse_args()
    files = [ROOT / name for name in ('assets/lapis-512gb.json', 'docs/recovery.md',
                                     'build/load_android_test', 'LICENSE', 'NOTICE.md',
                                     'tools/LICENSE.chromium', 'tools/LICENSE.apache-2.0',
                                     'tools/restore_fedora_boot.py', 'tools/restore_bootstrap.py', 'tools/native_fedora.py',
                                     'tools/prepare_fedora_boot.py')]
    files.extend(sorted((ROOT / 'lapis').glob('*.py')))
    from tools.build_fedora_usb import vendor_payload
    from lapis.common import runtime_sources
    avb_license = vendor_payload(runtime_sources(), ROOT)['avb/LICENSE']
    for file in files:
        require_regular(file)
    dest = new_regular_output(args.output)
    with dest.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
        for file in files:
            data = file.read_bytes()
            info = tarfile.TarInfo('lapis-recovery-kit/' + str(file.relative_to(ROOT)))
            info.size = len(data)
            info.mode = 0o755 if file.name == 'load_android_test' else 0o644
            archive.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo('lapis-recovery-kit/vendor/avb/LICENSE')
        info.size = len(avb_license)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(avb_license))
    print(dest)


if __name__ == '__main__':
    main()
