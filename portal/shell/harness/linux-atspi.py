#!/usr/bin/env python3
"""CPU-only native shell/AT-SPI/Orca smoke. Not a human intelligibility pass.

Usage: python3 shell/harness/linux-atspi.py --artifacts ~/.cache/bob-e12/ui --scenario allow
Requires Xvfb/xauth/xdotool, dbus-run-session, python-atspi, Orca and speech-dispatcher.
Build root binaries and shell with `cargo build --features tauri/custom-protocol` first.
Uses a private X display, session bus, settings/runtime dirs and a speech sink that
needs no PulseAudio (the espeak-ng module over a private ALSA `null` device).
Never points at the operator's DISPLAY, speech-dispatcher socket or audio sink.

What this measures, and what it does not (MVP3, review finding F-M2):

* `probe_state` is the only claim about the utterance measurement:
    MEASURED      a live screen reader was observed at the outcome moment, so
                  `live_utterance_generated` is a real true/false answer;
    PROBE_INVALID the reader was not live at the outcome (it had already
                  announced "screen reader off"), so the utterance question is
                  unanswerable for this run - NOT a product failure;
    UNMEASURED    the reader never reached AT-SPI at all.
* `reader_live_at_result` is derived from the reader's own debug log (no
  shutdown marker) plus process liveness - never from `poll()` alone.
* `status_showing` is informational only: the SHOWING bit is an incidental
  clipping state, not evidence about whether the status region is exposed.
* Human intelligibility of synthesized speech is NOT TESTED here.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--artifacts', required=True, type=Path)
parser.add_argument('--scenario', choices=['allow', 'deny', 'mismatch'], default='allow')
parser.add_argument('--require-live-utterance', action='store_true', help='Fail unless a live reader generates the outcome utterance; an explicit unclosed acceptance gate')
parser.add_argument('--reader-restarts', type=int, default=3, help='how often to restart a reader that exits before the outcome (recorded, never hidden)')
parser.add_argument('--utterance-wait', type=float, default=6.0, help='seconds to wait for the outcome utterance after the result frame')
parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
parser.add_argument('--launch', default=None, help="command that launches the shell window (default: the debug shell binary). Use 'scripts/linux/run-preview.sh --no-build' to exercise the shipped launcher end to end.")
args = parser.parse_args()
root = args.artifacts.expanduser().resolve()
root.mkdir(parents=True, exist_ok=True)
repo = Path(__file__).resolve().parents[2]

if not args.inside:
    env = os.environ.copy()
    for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'AT_SPI_BUS_ADDRESS', 'DBUS_STARTER_ADDRESS', 'DBUS_STARTER_BUS_TYPE']:
        env.pop(key, None)
    for key, sub in [('XDG_RUNTIME_DIR', 'runtime'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'), ('XDG_DATA_HOME', 'data'), ('TMPDIR', 'tmp')]:
        directory = root / sub
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
        env[key] = str(directory)
    env.update({'GDK_BACKEND': 'x11', 'GTK_A11Y': 'always', 'GTK_MODULES': 'atk-bridge',
                'LIBGL_ALWAYS_SOFTWARE': '1', 'GALLIUM_DRIVER': 'llvmpipe',
                'WEBKIT_DISABLE_DMABUF_RENDERER': '1', 'WEBKIT_DISABLE_COMPOSITING_MODE': '1',
                'GSETTINGS_BACKEND': 'memory', 'BOB_E12_PRIVATE_DISPLAY': '1',
                'SPEECHD_ADDRESS': 'unix_socket:' + str(root / 'speechd.sock')})
    cmd = ['xvfb-run', '-a', '-f', str(root / 'Xauthority'), '-s', '-screen 0 1280x1100x24 -nolisten tcp',
           'dbus-run-session', '--', sys.executable, str(Path(__file__).resolve()),
           '--inside', '--artifacts', str(root), '--scenario', args.scenario,
           '--reader-restarts', str(args.reader_restarts), '--utterance-wait', str(args.utterance_wait)]
    if args.require_live_utterance: cmd.append('--require-live-utterance')
    if args.launch: cmd += ['--launch', args.launch]
    sys.exit(subprocess.run(cmd, env=env, timeout=180).returncode)

assert os.environ.get('BOB_E12_PRIVATE_DISPLAY') == '1'
assert os.environ.get('DISPLAY') and os.environ.get('DBUS_SESSION_BUS_ADDRESS')
assert Path(os.environ['XDG_RUNTIME_DIR']).parent == root
# Import only after the wrapper has isolated both display and D-Bus.
import pyatspi  # noqa: E402

os.environ.update({'BOB_SLICE_CLI': str(repo / 'target/debug/bob-slice-cli'),
                   'BOB_FX_READ': str(repo / 'target/debug/fx-read')})
processes = []
logs = []
def launch(cmd, name):
    log = open(root / (name + '.log'), 'w')
    logs.append(log)
    child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    processes.append(child)
    return child

# A real Orca observes native AT-SPI and generates utterances. The speech sink
# must survive a host with no PulseAudio (the review's dummy module died with
# `pa_simple_new() failed: Connection refused`, which is why the old probe's
# "no utterance" could not be trusted): the espeak-ng module renders into a
# private ALSA `null` device, so nothing is played on the operator's hardware
# and no audio server is required.
conf = root / 'speech-config'
conf.mkdir(exist_ok=True)
(conf / 'speechd.conf').write_text(
    'LogLevel 3\n'
    'DisableAutoSpawn\n'
    'AudioOutputMethod "alsa"\n'
    'AddModule "espeak-ng" "sd_espeak-ng" "espeak-ng.conf"\n'
    'DefaultModule espeak-ng\n')
alsa = root / 'alsa'
alsa.mkdir(exist_ok=True)
(alsa / 'asound.conf').write_text('pcm.!default { type null }\nctl.!default { type null }\n')
os.environ['ALSA_CONFIG_PATH'] = str(alsa / 'asound.conf')
speech = launch(['speech-dispatcher', '-s', '-C', str(conf), '-S', str(root / 'speechd.sock'),
                 '-P', str(root / 'speechd.pid'), '-L', str(root)], 'speechd')
for _ in range(50):
    if (root / 'speechd.sock').exists() or speech.poll() is not None:
        break
    time.sleep(.1)

# ---- reader supervision (F-M2) -------------------------------------------
# A reader that dies mid-run makes the utterance question unanswerable. Watch
# it, restart it if it exits early (each restart is recorded, never hidden) and
# judge liveness from the debug log rather than from poll() alone.
SHUTDOWN_MARKERS = ('ORCA: GLib handler received shutdown signal',
                    'ORCA: Shutdown complete',
                    "SPEECH OUTPUT: 'Screen reader off.'")
reader = {'instances': [], 'debug': None, 'child': None, 'stop': threading.Event()}

def reader_supervisor():
    index = 0
    while not reader['stop'].is_set():
        debug = root / 'orca.debug' if index == 0 else root / f'orca-{index}.debug'
        child = launch(['orca', '--debug-file', str(debug)], 'orca' if index == 0 else f'orca-{index}')
        entry = {'index': index, 'pid': child.pid, 'debug': str(debug), 'started': time.strftime('%H:%M:%S')}
        reader['instances'].append(entry)
        reader['debug'] = debug
        reader['child'] = child
        child.wait()
        entry['exit'] = child.returncode
        entry['exited_at'] = time.strftime('%H:%M:%S')
        if reader['stop'].is_set() or index >= args.reader_restarts:
            break
        index += 1
        time.sleep(.5)

reader_thread = threading.Thread(target=reader_supervisor, daemon=True)
reader_thread.start()
time.sleep(1)
launch_cmd = shlex.split(args.launch) if args.launch else [str(repo / 'shell/src-tauri/target/debug/bob-slice-shell')]
app = launch(launch_cmd, 'app')

def nodes():
    out = []
    def walk(node, depth=0):
        if depth > 40:
            return
        try:
            out.append(node)
            for index in range(node.childCount):
                walk(node.getChildAtIndex(index), depth + 1)
        except Exception:
            pass
    walk(pyatspi.Registry.getDesktop(0))
    return out

def tree(name):
    result = []
    for node in nodes():
        try:
            entry = {'attributes': node.getAttributes(), 'role': node.getRoleName(), 'name': node.name,
                     'states': [str(x) for x in node.getState().getStates()]}
            try:
                entry['text'] = node.queryText().getText(0, -1)
            except Exception:
                pass
            result.append(entry)
        except Exception:
            pass
    (root / (name + '.json')).write_text(json.dumps(result, indent=2))
    return result

def find(name, prefix=False):
    for node in nodes():
        try:
            if node.name == name or (prefix and node.name.startswith(name)):
                return node
        except Exception:
            pass
    return None

def wait_for(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        if app.poll() is not None:
            raise RuntimeError('shell exited ' + str(app.returncode))
        time.sleep(.1)
    raise AssertionError('native UI condition timed out')

def key(value):
    subprocess.run(['xdotool', 'key', '--clearmodifiers', value], check=True)
    time.sleep(.15)

def focus(node):
    assert node.queryComponent().grabFocus(), 'native focus request refused'
    wait_for(lambda: node.getState().contains(pyatspi.STATE_FOCUSED))

def screenshot(name):
    subprocess.run(['import', '-window', 'root', str(root / (name + '.png'))], check=True, timeout=10)

def reader_debug_text():
    """The live reader's debug log, truncated at snapshot time."""
    path = reader['debug']
    if path is None:
        return ''
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return ''

def shutdown_lines(text):
    return [line for line in text.splitlines() if any(marker in line for marker in SHUTDOWN_MARKERS)]

def utterances(text):
    found = []
    for line in text.splitlines():
        if 'SPEECH OUTPUT: ' in line:
            found.append(line.split('SPEECH OUTPUT: ', 1)[1])
    return found

try:
    opener = wait_for(lambda: find('Open session and begin the turn'))
    print('NATIVE_INITIAL_ROLE', opener.getRoleName(), flush=True)
    tree('initial')
    win = subprocess.check_output(['xdotool', 'search', '--onlyvisible', '--name', 'BoB slice']).decode().splitlines()[-1]
    subprocess.run(['xdotool', 'windowfocus', '--sync', win], check=True)
    focus(opener)
    key('Return')
    allow = wait_for(lambda: find('allow_once'))
    assert allow.getRoleName() == 'button'
    tree('approval')
    screenshot('approval')
    if args.scenario == 'mismatch':
        checkbox = wait_for(lambda: find('Corrupt the args_digest echo', prefix=True))
        focus(checkbox); key('space')
        focus(allow); key('Return')
        wait_for(lambda: any('approval_mismatch' in x.get('text', '') for x in tree('mismatch')))
        assert allow.getState().contains(pyatspi.STATE_ENABLED), 'mismatch closed the pending request'
        focus(checkbox); key('space')
    focus(allow)
    expected = 'succeeded'
    if args.scenario == 'deny':
        key('Tab')
        deny = wait_for(lambda: find('deny'))
        assert deny.getState().contains(pyatspi.STATE_FOCUSED), 'Tab must reach deny'
        key('Return')
        wait_for(lambda: any(n.getRoleName() == 'entry' and n.getState().contains(pyatspi.STATE_FOCUSED) for n in nodes()))
        subprocess.run(['xdotool', 'type', '--clearmodifiers', '--', 'E12 native keyboard reason'], check=True)
        key('Tab')
        send = wait_for(lambda: find('Send deny with reason'))
        assert send.getState().contains(pyatspi.STATE_FOCUSED), 'Tab must reach the deny submission'
        key('Return')
        expected = 'denied'
    else:
        key('Return')
    wait_for(lambda: any(x.get('text', '').startswith('tool.result: ' + expected) for x in tree('current')))
    result_seen = time.strftime('%H:%M:%S')
    final = tree('result')
    assert not find('allow_once').getState().contains(pyatspi.STATE_ENABLED)
    assert not find('deny').getState().contains(pyatspi.STATE_ENABLED)
    screenshot('result')
    # Give a live reader the chance to announce the status change: the previous
    # probe sampled immediately and could not distinguish "queued" from "never".
    deadline = time.monotonic() + args.utterance_wait
    outcome_utterance = False
    while time.monotonic() < deadline:
        text = reader_debug_text()
        outcome_utterance = any('tool.result: ' + expected in line for line in utterances(text))
        if outcome_utterance or shutdown_lines(text):
            break
        time.sleep(.1)
    debug = reader_debug_text()
    shut = shutdown_lines(debug)
    instance = reader['instances'][-1] if reader['instances'] else None
    reader_alive = reader['child'] is not None and reader['child'].poll() is None
    live_at_result = bool(reader_alive and not shut)
    spoke_at_all = any('SPEECH OUTPUT: ' in line for line in debug.splitlines())
    if not spoke_at_all:
        probe_state = 'UNMEASURED'
    elif not live_at_result:
        probe_state = 'PROBE_INVALID'
    else:
        probe_state = 'MEASURED'
    speechd_log = (root / 'speechd.log').read_text(errors='replace') if (root / 'speechd.log').exists() else ''
    summary = {
        'probe_state': probe_state,
        'live_utterance_generated': outcome_utterance if probe_state == 'MEASURED' else None,
        'reader_live_at_result': live_at_result,
        'reader_instances': len(reader['instances']),
        'reader_restarts': max(0, len(reader['instances']) - 1),
        'reader_instance_debug': instance['debug'] if instance else None,
        'reader_shutdown_observed': bool(shut),
        'reader_shutdown_marker': shut[0] if shut else None,
        'outcome_utterances': [u for u in utterances(debug) if 'tool.result: ' in u][-3:],
        'result_seen_at': result_seen,
        'utterance_wait_seconds': args.utterance_wait,
        'status_showing': any(n.getRoleName() == 'status bar' and n.getState().contains(pyatspi.STATE_SHOWING) for n in nodes()),
        'status_showing_note': 'informational only: the SHOWING bit is an incidental clipping state (F-M2)',
        'scenario': args.scenario,
        'native_result': expected,
        'human_intelligibility': 'NOT_TESTED',
        'speech_sink': 'espeak-ng module rendered into a private ALSA null device (no PulseAudio, no operator audio)',
        'speechd_module_crashed': 'Output module terminated abnormally' in speechd_log,
        'speechd_pulse_error': 'pa_simple_new() failed' in speechd_log,
        'display': os.environ['DISPLAY'],
        'backend_visible': any('linux_bubblewrap' in x.get('text', '') for x in final),
        'private_runtime': os.environ['XDG_RUNTIME_DIR'],
    }
    (root / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    if args.require_live_utterance:
        assert probe_state == 'MEASURED', (
            f'cannot accept or reject the utterance gate: probe_state={probe_state} '
            f'(reader live at result: {live_at_result}, shutdown marker: {summary["reader_shutdown_marker"]})')
        assert outcome_utterance, 'a live reader did not announce the outcome'
finally:
    reader['stop'].set()
    for child in reversed(processes):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    for log in logs:
        log.close()
    print('APP_LOG', (root / 'app.log').read_text(errors='replace'), flush=True)
