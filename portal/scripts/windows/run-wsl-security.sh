#!/bin/sh
set -eu
umask 077
base=/root/bob-windows-ui-review-20261024/test-data
package=${BOB_VALIDATION_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)}
mkdir -p "$base"
export TMPDIR="$base"
export BOB_WSL_ENGINE=1
for suite in functional_security functional_project functional_approval functional_resume functional_limits project_context usability_organization usability_attachments wsl_root_identity; do
 printf '\n=== ACTUAL WSL %s (deterministic fixtures, no provider) ===\n' "$suite"
 "$package/sprint-evidence/wsl-test-bins/$suite.test" --test-threads=1
 done
printf '\nALL NINE WSL deterministic suites completed\n'
