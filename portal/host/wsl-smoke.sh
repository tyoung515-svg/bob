#!/bin/sh
set -eu
umask 077
base=/root/bob-windows-ui-review-20261024
package=${BOB_VALIDATION_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)}
mkdir -p "$base/test-data" "$base/demo-project"
export TMPDIR="$base/test-data"
if test ! -f "$base/demo-project/notes.txt"; then printf 'Windows WSL review\nThe synthetic project code is ORCHID-742.\n' > "$base/demo-project/notes.txt"; fi
python3 "$package/host/host_acceptance.py" "$package/release/portable/bob-wsl-host"
python3 "$package/host/host_boundary.py" "$package/release/portable/bob-wsl-host" "$package/sprint-evidence"
printf 'WSL helper actual runtime PASS; no model dispatches\n'
