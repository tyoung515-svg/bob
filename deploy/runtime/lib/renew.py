"""Root timer entrypoint; one locked bob-lane invocation per active lane."""
import subprocess
from common import require_root
from lane import lanes


def main():
    require_root()
    failed = False
    from common import migration_pending as pending
    for name, path in lanes():
        if (path.parent / '.disabled').exists() or pending(name):
            continue
        result = subprocess.run(['/usr/local/bin/bob-lane', 'renew-if-due', name], timeout=60)
        failed = failed or result.returncode != 0
    return int(failed)
