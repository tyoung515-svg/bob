"""Execution mode shared by every core execution boundary.

The packaged image's root-owned marker is authoritative. Environment can opt
into the boundary, but cannot opt out of an installed marker. Detection reads
no worker configuration; legacy hosts never need /etc/bob to be provisioned.
"""
import os
from pathlib import Path


EXECUTION_BOUNDARY = Path('/etc/bob/execution-boundary')


def mode() -> str:
    # lexists includes broken symlinks. Permission/stat errors fail closed too.
    # Do not cache: changing the environment cannot invalidate the marker.
    try:
        EXECUTION_BOUNDARY.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return 'worker'
    else:
        return 'worker'
    return 'worker' if os.environ.get('BOB_EXECUTION_BOUNDARY') == 'worker' else 'legacy'
