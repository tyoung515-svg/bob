#!/usr/bin/env bash
# Native candidate launch only: no build, migration, listener, or remote probe.
set -euo pipefail
candidate=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
target_dir=${BOB_PORTAL_TARGET_DIR:-"$candidate/shell/src-tauri/target"}
binary=${BOB_PORTAL_BINARY:-"$target_dir/release/bob-slice-shell"}
if [[ "$binary" != "$candidate/"* || "$binary" == *'/../'* ]]; then
    printf 'Portal binary must be inside this candidate: %s\n' "$binary" >&2
    exit 2
fi
if [[ ! -f "$binary" || ! -x "$binary" ]]; then
    printf 'Portal binary missing or not executable: %s\nBuild this candidate first.\n' "$binary" >&2
    exit 2
fi
# Resolve symlinks before accepting the candidate boundary.
binary=$(realpath -e -- "$binary")
if [[ "$binary" != "$candidate/"* ]]; then
    printf 'Portal binary resolves outside this candidate: %s\n' "$binary" >&2
    exit 2
fi
export BOB_PORTAL_DATA_ROOT=${BOB_PORTAL_DATA_ROOT:-"$candidate/.portal-linux-data"}
if [[ "$BOB_PORTAL_DATA_ROOT" != /* ]]; then
    printf 'BOB_PORTAL_DATA_ROOT must be absolute\n' >&2
    exit 2
fi
export BOB_FUNCTIONAL_DATA_ROOT=${BOB_FUNCTIONAL_DATA_ROOT:-"$BOB_PORTAL_DATA_ROOT/local-workbench"}
export BOB_PREVIEW_DATA_ROOT=${BOB_PREVIEW_DATA_ROOT:-"$BOB_PORTAL_DATA_ROOT/slice-preview"}
machine_config="$candidate/.portal-local/ssh.conf"
default_target=
if [[ -z "${BOB_PORTAL_SSH_CONFIG:-}" && -f "$machine_config" ]]; then
    export BOB_PORTAL_SSH_CONFIG="$machine_config"
    default_target=bob-portal-server
fi
if [[ -n "${BOB_PORTAL_SSH_CONFIG:-}" && ( "$BOB_PORTAL_SSH_CONFIG" != /* || ! -f "$BOB_PORTAL_SSH_CONFIG" ) ]]; then
    printf 'BOB_PORTAL_SSH_CONFIG must be an existing absolute file\n' >&2
    exit 2
fi
export BOB_PORTAL_TARGET=${BOB_PORTAL_TARGET:-$default_target}
# User and broker path come from saved settings or explicit environment overrides.
umask 077
mkdir -p -- "$BOB_PORTAL_DATA_ROOT"
chmod 700 -- "$BOB_PORTAL_DATA_ROOT"
# flock owns the lock for this process lifetime; different roots stay independent.
exec 9>"$BOB_PORTAL_DATA_ROOT/.portal-launch.lock"
if ! flock -n 9; then
    printf 'Portal already running for data root: %s\n' "$BOB_PORTAL_DATA_ROOT" >&2
    exit 3
fi
exec "$binary" "$@"
