#!/usr/bin/env bash
# BoB slice — local Linux preview launcher (EXPERIMENTAL).
#
# One window, one call: the existing scripted FX-READ-01 fixture read through
# the real engine, authority gate, bubblewrap executor and durable ledger.
#
#   ./scripts/linux/run-preview.sh --check        # prereqs only, no build/run
#   ./scripts/linux/run-preview.sh                # build if needed, then launch
#   ./scripts/linux/run-preview.sh --no-build     # launch what is already built
#   ./scripts/linux/run-preview.sh --headless     # no GUI: real CLI demo in a terminal
#   ./scripts/linux/run-preview.sh --build-only   # build engine + frontend + shell
#
# Everything this script writes lands in a fresh per-run directory under
# $XDG_CACHE_HOME/bob-preview/runs/ (override with --data-root or
# BOB_PREVIEW_RUN_ROOT). It never uses sudo, never opens a network listener,
# never installs a system package, never starts a background service and never
# weakens the sandbox: if bubblewrap cannot run, the preview refuses to start.
#
# Exit codes (typed; see TINKER.md):
#   0 ok            2 missing build tool        3 unusable/missing bubblewrap
#   4 missing engine or shell binary            5 data-root problem
#   6 no graphical display                      7 build failed
#   8 frontend dependencies missing            64 usage error
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$script_dir/../.." && pwd)"

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

say()  { printf 'run-preview: %s\n' "$*"; }
fail() { # fail <exit-code> <TYPED_CODE> <message...>
  local code="$1" typed="$2"; shift 2
  printf 'run-preview: %s: %s\n' "$typed" "$*" >&2
  exit "$code"
}

mode="gui"; do_build=1; build_only=0; check_only=0; capture=0; print_env=0
data_root=""; install_frontend=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check)            check_only=1 ;;
    --no-build)         do_build=0 ;;
    --build-only)       build_only=1 ;;
    --headless)         mode="headless" ;;
    --data-root)        shift; data_root="${1:-}" ;;
    --data-root=*)      data_root="${1#*=}" ;;
    --install-frontend) install_frontend=1 ;;
    --capture)          capture=1 ;;
    --print-env)        print_env=1 ;;
    -h|--help)          usage; exit 0 ;;
    *)                  usage; fail 64 USAGE_ERROR "unknown argument: $1" ;;
  esac
  shift
done

# F-L2: usage conflicts are decided before any prerequisite probing, so
# `--build-only --no-build` is a usage error (64), not "no display" (6).
if [ "$build_only" = 1 ] && [ "$do_build" = 0 ]; then
  usage
  fail 64 USAGE_ERROR "--build-only conflicts with --no-build"
fi

target_dir="${BOB_PREVIEW_TARGET_DIR:-$repo/target}"
cli_bin="$target_dir/debug/bob-slice-cli"
fx_bin="$target_dir/debug/fx-read"
shell_bin="$repo/shell/src-tauri/target/debug/bob-slice-shell"
bwrap_bin="${BOB_BWRAP:-$(command -v bwrap || true)}"

# ---------------------------------------------------------------- prerequisites
# One binary at a time, each with a typed code and a concrete remedy.
want_tool() { # want_tool <name> <path> <when: always|build|gui>
  local name="$1" found="$2" when="$3"
  [ -n "$found" ] || fail 2 "PREREQ_MISSING_${name^^}" \
    "$name not found on PATH (needed for: $when). Install it with your OS package manager; this launcher installs nothing."
  printf '  %-14s %s\n' "$name" "$found"
}

say "checking prerequisites (repo: $repo)"
if [ "$do_build" = 1 ]; then
  want_tool cargo "$(command -v cargo || true)" build
  want_tool rustc "$(command -v rustc || true)" build
  want_tool npm   "$(command -v npm   || true)" build
  want_tool node  "$(command -v node  || true)" build
  if [ ! -d "$repo/shell/node_modules" ] && [ "$install_frontend" = 0 ]; then
    if [ "$check_only" = 1 ]; then
      # F-L5: --check answers "are the prereqs here?" for every prereq instead
      # of stopping at the first one on a fresh clone.
      printf '  %-14s %s\n' "frontend deps" \
        "absent — warning only for --check; (cd shell && npm ci) is needed before a build or launch"
    else
      fail 8 PREREQ_MISSING_FRONTEND_DEPS \
        "$repo/shell/node_modules is absent. Run: (cd shell && npm ci)   — or re-run with --install-frontend to let this launcher do exactly that."
    fi
  fi
fi

[ -n "$bwrap_bin" ] || fail 3 PREREQ_MISSING_BWRAP \
  "bubblewrap (bwrap) not found. The real executor needs it; there is no unsandboxed fallback. Install it with your OS package manager."
[ -x "$bwrap_bin" ] || fail 3 PREREQ_MISSING_BWRAP "$bwrap_bin is not executable"
printf '  %-14s %s (%s)\n' bwrap "$bwrap_bin" "$("$bwrap_bin" --version 2>/dev/null | head -1)"

# A real namespace probe, so a host that cannot run the sandbox fails here with
# a typed message instead of inside the app. Same read roots the executor binds
# (the loader needs /lib64 too); nothing is widened or relaxed for the check.
probe_binds=(--ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /bin /bin)
[ -e /lib64 ] && probe_binds+=(--ro-bind /lib64 /lib64)
if ! "$bwrap_bin" --unshare-all --die-with-parent --cap-drop ALL \
      "${probe_binds[@]}" --proc /proc --dev /dev --remount-ro / /bin/true >/dev/null 2>&1; then
  fail 3 PREREQ_SANDBOX_UNUSABLE \
    "bwrap cannot create a user/PID/net namespace on this host (unprivileged user namespaces disabled?). The preview refuses to run without real isolation."
fi
printf '  %-14s %s\n' sandbox "user/pid/net namespace probe: ok"

if [ "$mode" = "gui" ] && [ "$check_only" = 0 ] && [ "$print_env" = 0 ]; then
  if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    fail 6 PREREQ_NO_DISPLAY \
      "no DISPLAY/WAYLAND_DISPLAY. On a headless host use: $0 --headless   (real CLI demo, no window)"
  fi
  printf '  %-14s %s\n' display "${WAYLAND_DISPLAY:+wayland $WAYLAND_DISPLAY}${DISPLAY:+x11 $DISPLAY}"
elif [ "$mode" = "gui" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  printf '  %-14s %s\n' display "none (a GUI launch would fail; --headless works here)"
fi
want_tool python3 "$(command -v python3 || true)" headless-demo

if [ "$do_build" = 0 ]; then
  [ -x "$cli_bin" ]  || fail 4 PREREQ_MISSING_ENGINE_BINARIES "missing $cli_bin (build with: cargo build at the repo root, or drop --no-build)"
  [ -x "$fx_bin" ]   || fail 4 PREREQ_MISSING_ENGINE_BINARIES "missing $fx_bin (build with: cargo build at the repo root, or drop --no-build)"
  [ -x "$shell_bin" ]|| fail 4 PREREQ_MISSING_SHELL_BINARY "missing $shell_bin (build with: (cd shell/src-tauri && cargo build --features tauri/custom-protocol), or drop --no-build)"
fi

# ---------------------------------------------------------------- build
build() {
  say "building engine (cargo build) — first build can take a few minutes"
  ( cd "$repo" && CARGO_TARGET_DIR="$target_dir" cargo build ) || fail 7 BUILD_FAILED "engine build failed"
  say "building frontend (npm run build in shell/)"
  ( cd "$repo/shell" && npm run build ) || fail 7 BUILD_FAILED "frontend build failed"
  say "building shell (cargo build --features tauri/custom-protocol, embeds the frontend)"
  ( cd "$repo/shell/src-tauri" && cargo build --features tauri/custom-protocol ) \
    || fail 7 BUILD_FAILED "shell build failed"
}

if [ "$check_only" = 1 ]; then
  say "check ok (no build, no launch)"
  exit 0
fi

if [ "$build_only" = 1 ]; then
  build
  [ -x "$cli_bin" ] && [ -x "$fx_bin" ] && [ -x "$shell_bin" ] || fail 4 PREREQ_MISSING_ENGINE_BINARIES "build finished but a binary is missing"
  say "build ok: $cli_bin $fx_bin $shell_bin"
  exit 0
fi

# Stay under the build gate if the caller asked for none.
if [ "$do_build" = 1 ]; then build; fi
[ -x "$cli_bin" ]   || fail 4 PREREQ_MISSING_ENGINE_BINARIES "missing $cli_bin"
[ -x "$fx_bin" ]    || fail 4 PREREQ_MISSING_ENGINE_BINARIES "missing $fx_bin"
[ -x "$shell_bin" ] || fail 4 PREREQ_MISSING_SHELL_BINARY "missing $shell_bin"

# ---------------------------------------------------------------- fresh run dir
run_root="${BOB_PREVIEW_RUN_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/bob-preview/runs}"
stamp="$(LC_ALL=C date +%Y%m%dT%H%M%S)"
used_data_root=0
if [ -n "$data_root" ]; then
  used_data_root=1
  run_dir="$data_root"
  if [ -e "$run_dir" ] && [ -n "$(ls -A "$run_dir" 2>/dev/null)" ]; then
    fail 5 PREREQ_DATA_ROOT_NOT_FRESH \
      "$run_dir is not empty. Every preview run gets a fresh data root; pick a new --data-root."
  fi
else
  run_dir="$run_root/${stamp}-${mode}-$$"
fi
mkdir -p "$run_dir" || fail 5 PREREQ_DATA_ROOT_UNWRITABLE "cannot create $run_dir"
[ -w "$run_dir" ] || fail 5 PREREQ_DATA_ROOT_UNWRITABLE "$run_dir is not writable"
data_dir="$run_dir/data"
headless_dir="$run_dir/headless"

head_sha="$(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)"
branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

# run.json is mode-aware and derived from the directory this run actually
# writes (F-M1, F-M4). `--headless` gives every scenario its own data root
# under headless/<scenario>/data, so naming `<run>/data` there would send a
# reader to an empty directory; the reset note names the run (and the shared
# run root only when the default root was used).
write_run_json() { # write_run_json <phase: start|end> [exit-code]
  python3 - "$run_dir" "$mode" "$repo" "$branch" "$head_sha" "$cli_bin" "$fx_bin" \
    "$shell_bin" "$bwrap_bin" "$data_dir" "$run_root" "$used_data_root" "$1" "${2:-}" <<'PY'
import hashlib
import json
import sys
import time
from pathlib import Path

(run_dir, mode, repo, branch, head, cli, fx, shell, bwrap, data_dir, run_root,
 used_data_root, phase, exit_code) = sys.argv[1:15]
run_dir, data_dir = Path(run_dir), Path(data_dir)
path = run_dir / 'run.json'
doc = json.loads(path.read_text()) if path.exists() else {}


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


if mode == 'gui':
    roots = [data_dir]
    doc['data_root'] = str(data_dir)
    doc['data_root_note'] = 'the GUI run writes its ledger here'
else:
    roots = sorted(p for p in (run_dir / 'headless').glob('*/data') if p.is_dir())
    doc['data_root'] = None
    doc['data_root_note'] = ('no single data root: every scenario owns a fresh one under '
                             'headless/<scenario>/data')
doc['kind'] = '%s preview run (scripted single fixture read, no model, no network)' % mode
doc['mode'] = mode
doc['repo'] = repo
doc['branch'] = branch
doc['head'] = head
doc['binaries'] = {'bob-slice-cli': cli, 'fx-read': fx, 'bob-slice-shell': shell, 'bwrap': bwrap}
doc['sha256'] = {'bob-slice-cli': sha256(cli), 'fx-read': sha256(fx), 'bob-slice-shell': sha256(shell)}
doc['data_roots'] = [str(p) for p in roots]
doc['ledger_pattern'] = 'ledgers/<session_id>/segments/<segment_id>.audit.jsonl'
doc['ledger_files'] = [str(f) for p in roots for f in sorted(p.glob('ledgers/*/segments/*.audit.jsonl'))]
if used_data_root == '1':
    doc['note'] = 'Reset ONLY this preview data: delete %s (never the repo or your home).' % run_dir
else:
    doc['note'] = ('Reset ONLY preview data: delete %s to remove this run, or %s to clear every '
                   'preview run (never the repo or your home).' % (run_dir, run_root))
doc.setdefault('started', time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime()))
doc['phase'] = phase
if exit_code != '':
    doc['exit'] = int(exit_code)
    doc['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime())
path.write_text(json.dumps(doc, indent=2, sort_keys=True) + '\n')
PY
}

if [ "$mode" = "gui" ]; then
  mkdir -p "$data_dir" || fail 5 PREREQ_DATA_ROOT_UNWRITABLE "cannot create $data_dir"
fi
write_run_json start || fail 5 PREREQ_DATA_ROOT_UNWRITABLE "cannot write $run_dir/run.json"

export BOB_DEVELOPER_FIXTURE=1
export BOB_SLICE_CLI="$cli_bin"
export BOB_FX_READ="$fx_bin"
export BOB_PREVIEW_DATA_ROOT="$data_dir"

say "run dir:   $run_dir"
if [ "$mode" = "headless" ]; then
  say "data roots: one per scenario under $headless_dir/<scenario>/data"
else
  say "data root: $data_dir  (ledger under ledgers/<session>/segments/)"
fi
say "binaries:  $cli_bin"

# F-M3: a session-like runtime dir. Without one (or with a wider-than-0700 one)
# the Tauri/WebKit window never appears and nothing says why, so the launcher
# provides a private one under the run directory instead of hanging silently.
if [ "$mode" = "gui" ]; then
  current_runtime="${XDG_RUNTIME_DIR:-}"
  if [ -z "$current_runtime" ] || [ ! -d "$current_runtime" ] \
     || [ "$(stat -c %a "$current_runtime" 2>/dev/null)" != "700" ]; then
    mkdir -p -m 700 "$run_dir/runtime" || fail 5 PREREQ_RUNTIME_DIR_UNWRITABLE \
      "cannot create a private runtime dir under $run_dir"
    chmod 700 "$run_dir/runtime" || fail 5 PREREQ_RUNTIME_DIR_UNWRITABLE \
      "cannot chmod 0700 $run_dir/runtime"
    export XDG_RUNTIME_DIR="$run_dir/runtime"
    say "runtime dir: $XDG_RUNTIME_DIR (private 0700; the inherited XDG_RUNTIME_DIR was unset or not 0700)"
  else
    printf '  %-14s %s\n' runtime "$current_runtime"
  fi
fi

if [ "$print_env" = 1 ]; then
  printf 'BOB_SLICE_CLI=%q\nBOB_FX_READ=%q\nBOB_PREVIEW_DATA_ROOT=%q\n' \
    "$BOB_SLICE_CLI" "$BOB_FX_READ" "$BOB_PREVIEW_DATA_ROOT"
  [ -n "${XDG_RUNTIME_DIR:-}" ] && printf 'XDG_RUNTIME_DIR=%q\n' "$XDG_RUNTIME_DIR"
  printf 'exec %q\n' "$shell_bin"
  exit 0
fi

# ---------------------------------------------------------------- run
if [ "$mode" = "headless" ]; then
  say "headless demo: real bob-slice-cli, real bwrap executor, real protocol (no window)"
  set +e
  python3 "$repo/scripts/linux/headless-demo.py" --repo "$repo" --cli "$cli_bin" \
    --fx-read "$fx_bin" --run-dir "$headless_dir" ${BOB_HEADLESS_ARGS:-}
  rc=$?
  set -e
  write_run_json end "$rc" || say "warning: could not finish $run_dir/run.json"
  say "headless demo exit $rc; artifacts: $headless_dir"
  exit "$rc"
fi

say "launching the experimental shell (close the window to end the preview)"
say "this is NOT a tier-1 accessibility or platform certification (see TINKER.md)"
if [ "$capture" = 1 ]; then
  "$shell_bin" > "$run_dir/shell.log" 2>&1
else
  "$shell_bin"
fi
rc=$?
write_run_json end "$rc" || say "warning: could not finish $run_dir/run.json"
say "shell exited $rc; run dir kept at $run_dir"
exit "$rc"
