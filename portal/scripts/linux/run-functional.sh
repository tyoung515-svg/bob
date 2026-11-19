#!/usr/bin/env bash
# App-local P1 launcher. Never changes desktop, drivers, provider config or owner data.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build=1;only=0;check=0;rendering=safe
while (($#)); do
 case "$1" in
 --no-build) build=0;;
 --build-only) only=1;;
 --check) check=1;;
 --data-root) [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || { printf '%s\n' '--data-root requires a private directory' >&2;exit 64; };shift;export BOB_FUNCTIONAL_DATA_ROOT="$1";;
 --rendering) [[ $# -ge 2 ]] || { printf '%s\n' '--rendering requires safe or native' >&2;exit 64; };shift;rendering="$1";;
 --help) printf '%s\n' 'Usage: scripts/linux/run-functional.sh [--check|--build-only|--no-build] [--data-root PRIVATE_DIR] [--rendering safe|native]' 'Default: isolated bob-workbench-p1 store; safe app-local WebKit/NVIDIA compatibility.' 'Explicit environment overrides are preserved, including empty values. native leaves existing rendering variables untouched.' 'No automatic session import, restart, provider fallback or global configuration changes.';exit 0;;
 *) printf 'Unknown argument %s\n' "$1" >&2;exit 64;;
 esac;shift
done
[[ "$rendering" == safe || "$rendering" == native ]] || { printf 'Unknown rendering mode %s\n' "$rendering" >&2;exit 64; }
export BOB_FUNCTIONAL_DATA_ROOT="${BOB_FUNCTIONAL_DATA_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/bob-workbench-p1}"
if [[ "$rendering" == safe ]];then
 export __NV_DISABLE_EXPLICIT_SYNC="${__NV_DISABLE_EXPLICIT_SYNC-1}"
 export WEBKIT_DISABLE_DMABUF_RENDERER="${WEBKIT_DISABLE_DMABUF_RENDERER-1}"
fi
unset BOB_DEVELOPER_FIXTURE
for tool in cargo npm node ssh timeout;do command -v "$tool" >/dev/null || { printf 'MISSING_TOOL: %s\n' "$tool" >&2;exit 2; };done
printf '%s\n' 'BoB workbench: local engine; temporary Bob / DeepSeek Flash model via trusted SSH.' 'Runtime/profile capability extensions are shown only when available. No provider credentials are copied to this PC.'
printf 'Data root: %s\nRendering: %s (explicit environment overrides preserved)\n' "$BOB_FUNCTIONAL_DATA_ROOT" "$rendering"
((check)) && exit 0
if ((only==0)) && [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]];then printf 'NO_DISPLAY: open from a graphical session (tests use private Xvfb).\n' >&2;exit 6;fi
if ((build));then
 [[ -d "$repo/shell/node_modules" ]] || { printf 'Run (cd shell && npm ci) first.\n' >&2;exit 8; }
 (cd "$repo" && cargo build)
 (cd "$repo/shell" && npm run build)
 (cd "$repo/shell/src-tauri" && cargo build --features tauri/custom-protocol)
fi
((only)) && exit 0
binary="$repo/shell/src-tauri/target/debug/bob-slice-shell"
[[ -x "$binary" ]] || { printf 'MISSING_BINARY: run --build-only first.\n' >&2;exit 4; }
if [[ -z "${XDG_RUNTIME_DIR:-}" ]];then
 runtime="${XDG_CACHE_HOME:-$HOME/.cache}/bob-workbench-p1/runtime-$$";mkdir -p -m 700 "$runtime";export XDG_RUNTIME_DIR="$runtime"
fi
exec "$binary"
