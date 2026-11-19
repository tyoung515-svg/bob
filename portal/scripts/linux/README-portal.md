Native Linux candidate launcher
===============================

Run `scripts/linux/launch-portal.sh` on the build machine after Parent builds and reviews
this candidate. It never builds, installs, probes SSH, migrates credentials, or
sets developer/fixture mode. No credentials belong in this candidate.

Default binary: `shell/src-tauri/target/release/bob-slice-shell` inside this
candidate. `BOB_PORTAL_TARGET_DIR` selects a private target directory;
`BOB_PORTAL_BINARY` selects an executable. Both must resolve inside the candidate.
A missing binary fails with exit 2. Arguments are forwarded literally.

Default launcher store: candidate-local `.portal-linux-data`. For private-store
smoke testing, set `BOB_PORTAL_DATA_ROOT=/absolute/private/test-root` and invoke
the same launcher. This is a normal native app launch; there is no fake smoke
mode. One launcher instance holds a kernel flock per root (exit 3 on contention).
Different roots are independent. Direct binary launches bypass that launcher lock.

Without the launcher override, native settings use `BOB_PORTAL_DATA_ROOT`, then
absolute `XDG_DATA_HOME/bob-portal`, then `HOME/.local/share/bob-portal`. Missing
absolute HOME/XDG without an override fails loudly. Directory permissions are
0700, settings 0600. Stale `.tmp` settings files fail closed rather than following
symlinks; inspect such a file before removing it. The launcher tightens the data
root, not every pre-existing file recursively; this is not an owner-data or
credential migration. Newly created settings/log files use their own permissions.

There is no default server target. If a private machine configuration exists at
`.portal-local/ssh.conf`, the launcher uses it through `BOB_PORTAL_SSH_CONFIG`
and defaults to its `bob-portal-server` alias instead. That file can reference an
EXISTING verified key/known-hosts entry; it must not contain copied provider tokens.
On Unix the selected config must be a regular, non-symlink file owned by the
current user or root and not writable by group/others. Included OpenSSH files
remain part of the operator's trusted SSH configuration, not model inputs.
`BOB_PORTAL_TARGET` and `BOB_PORTAL_BROKER` remain explicit operator overrides.
Strict host checking and batch mode remain mandatory. Server token/auth and all
portal effects stay on the server. Broker paths cannot contain shell syntax. No
automatic replay occurs. The launcher also isolates the dormant local workbench
and preview stores below the candidate data root; it does not open older workbench
stores merely because their native commands are registered.

The Linux platform configuration gives this port its own application identifier
and title, leaving Windows configuration unchanged. Rendering environment variables
are inherited only; no GPU/desktop settings are changed. Normal close/fault/final
drop kills and reaps the owned process group. On Linux, a dedicated spawn-owner
thread keeps a parent-death signal bound for the lifetime of the direct SSH child,
so an OOM/crash of the application also ends that direct transport. Arbitrary
remote/provider work and arbitrary grandchildren on abrupt death are NOT claimed
terminated. This is process supervision, not an OS sandbox.

Verification on the build machine (no server/model needed for Rust doubles):

```
cargo test --manifest-path shell/src-tauri/Cargo.toml portal
mkdir -p .portal-local/test-bin
rustc --edition=2021 --test shell/src-tauri/src/portal_unix.rs -o .portal-local/test-bin/portal-unix-tests
.portal-local/test-bin/portal-unix-tests --test-threads=1
python3 scripts/linux/tests/test_portal_port.py
bash -n scripts/linux/launch-portal.sh
```

The Python checks cover registration source and launcher behavior with a temporary
stub executable, not native runtime acceptance. Rust portal tests retain existing
EOF, fault, deadline, budget, no-retry and Windows inventory assertions; Linux
adds data-path, permissions and final Link-drop checks. The standalone Unix tests
exercise actual owned child/grandchild cleanup, idempotence, final-drop reaping,
and unrelated-child survival. `/proc` checks regard a killed orphan zombie as
stopped; the direct child must actually be reaped. Parent still needs Linux and
Windows builds plus real desktop/SSH/window-close verification on the build machine.
