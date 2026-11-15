"""Version-independent identification of Fedora Workstation live media."""
import re

from .common import require


def workstation_label(label):
    require(re.fullmatch(r'Fedora-WS-Live-[A-Za-z0-9._-]+', label) is not None,
            'Expected a Fedora Workstation live ISO (Fedora-WS-Live volume label)')
    return label


def validate_live_environment(release, cmdline, architecture):
    require(release.get('ID') == 'fedora' and release.get('VARIANT_ID') == 'workstation',
            'Boot the Fedora Workstation Lapis live USB first')
    require(architecture == 'x86_64', 'The Lapis live environment must be x86-64')
    arguments = cmdline.split()
    roots = [arg.removeprefix('root=live:CDLABEL=') for arg in arguments
             if arg.startswith('root=live:CDLABEL=')]
    require('rd.live.image' in arguments and len(roots) == 1,
            'Boot the Fedora Workstation Lapis live USB first')
    workstation_label(roots[0])
