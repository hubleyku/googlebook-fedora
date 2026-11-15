#!/usr/bin/env python3
"""Package pinned public tools for updating /opt/lapis-kit on installed Fedora."""
import argparse
import io
from pathlib import Path
import tarfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import ROOT, new_regular_output, runtime_sources
from tools.build_fedora_usb import kit_payload

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path, default=ROOT/'build/lapis-fedora-tools.tar.gz')
a = p.parse_args()
files = kit_payload(runtime_sources())
with new_regular_output(a.output).open('xb') as output, tarfile.open(fileobj=output,mode='w:gz') as archive:
    for name,(mode,data) in files.items():
        info=tarfile.TarInfo(name)
        info.mode=mode & 0o777
        if mode & 0o170000 == 0o040000:
            info.type=tarfile.DIRTYPE
            archive.addfile(info)
        else:
            info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
print(a.output)
