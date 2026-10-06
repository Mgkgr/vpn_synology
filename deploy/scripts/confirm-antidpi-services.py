#!/usr/bin/env python3
"""One interactive sudo, fixed independent isolated jobs; no production apply."""
import os
from pathlib import Path
import subprocess
import sys


BUILD = '07ed2a54bb6448d29aa659ba730395b6'
SERVICES = ('www.youtube.com', 'discord.com', 'web.telegram.org', 'www.instagram.com', 'www.wikipedia.org')


def run_batch(execute):
    result = 0
    for host in SERVICES:
        code = execute([sys.executable, '-B', str(Path(__file__).with_name('probe-antidpi-pinned-sites.py')),
                        '--approved', BUILD, 'confirm', host])
        if code not in (0, 2):
            return 1  # runtime / cleanup / invariance failure: no subsequent jobs
        if code == 2:
            result = 2  # negative site result is useful evidence, not a runtime failure
    return result


if __name__ == '__main__':
    if sys.argv[1:] != ['--approved'] or os.geteuid() != 0:
        sys.exit('Use --approved in the existing NAS root console.')
    sys.exit(run_batch(lambda args: subprocess.run(args, check=False).returncode))
