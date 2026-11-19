# In-place runtime upgrades

Build the image with `deploy/runtime/build.sh RELEASE_ID`. The final image step
generates `/etc/bob/runtime-payload.json` and `/etc/bob/runtime-version`.
The version contains the build ID, git revision, build epoch, and payload SHA256.
Build epoch is the release ordering key; use monotonically increasing epochs
(`SOURCE_DATE_EPOCH` may pin it for a release). Reusing a build ID is refused.

The one generated manifest inventories the permitted paths, entry types, modes,
symlink targets, sizes and file hashes. Its canonical SHA256 is the payload digest.
The image cannot contain its own compressed archive hash. The build therefore also
emits `dist/bob-runtime-RELEASE_ID.release.json`, which binds that payload/version
to the exported tarball's SHA256. Copy the tarball and release JSON into the app's
`dist-runtime/` as `bob-runtime.tar.gz` and `bob-runtime.release.json` when packaging
the installer. No runtime or installer is built by the offline upgrade tests.

## Operator commands (root inside BoB)

```
bob-runtime upgrade --payload /root/new-runtime.tar.gz --expected-sha256 HEX
bob-runtime upgrade --status
bob-runtime upgrade --resume UPGRADE_ID
bob-runtime upgrade --rollback UPGRADE_ID
```

`--force` overrides the same-version/downgrade gate only; all integrity, path,
space, isolation, health and migration-hash checks remain enforced. Only the latest
upgrade may be resumed or rolled back. A repeated payload/digest resumes an
unfinished journal; a different artifact is refused until recovery finishes.
An automatically rolled-back upgrade exits nonzero; a completed upgrade or a
successful explicit `--rollback` exits zero. JSON always states the outcome.

C4 predates these commands and its version marker. The desktop uses the existing
root WSL boundary to start a fixed systemd job. It privately copies the bundled
archive, verifies its outer digest, and loads only the two upgrade modules from
that verified archive. The same engine then validates the internal manifest and
payload. An absent installed marker is the explicit `legacy`, epoch-zero baseline.
The job survives closing the app; status polling on reconnect shows its phase.
Remote connections cannot invoke this local upgrade path. Upgrade never runs
automatically on launch/connection.

## Journal and recovery

Root-private `/var/lib/bob-upgrades/FROM-TO-ID/` retains the extracted payload,
original owned files, worker control backups, `bob.dump`, the phase journal, and
an independent recovery runner. Phase receipts and directory renames are flushed
before advancing. Runtime-owned trees are staged beside their destinations and
renamed while lane/core/gateway/bridge/worker/settings/renewal/embedder units are
stopped. Postgres, Redis and Qdrant remain running. Persistent systemd conditions
hold those runtime units off across a reboot during replacement or recovery.

The database is dumped once during backup and again after lane writers stop; the
second, quiescent snapshot is authoritative. SQL uses firstboot's existing ordered,
hashed, transactionally recorded migration runner. Its order is compatible with
core startup's execution migration order. Already recorded SQL is skipped;
changing an applied migration is refused.

Before SQL is attempted, failures restore files and start the original units.
Once SQL is attempted (including a partial batch), rollback restores files and
**replaces the entire `bobclaw` database from `bob.dump`**: it drops/recreates this
fixed-name database, then uses `pg_restore --single-transaction --exit-on-error`.
Recreation removes objects introduced by the failed release; `pg_restore --clean`
alone would leave them behind. Changes since that snapshot are lost, including
changes made after a successful upgrade if manual rollback is used later.
Database restore failure leaves a durable `rollback_failed` receipt and boot
inhibition, and can be retried. Do not delete the journal or backup to clear it.

If rollback restored C4's CLI, or a crash interrupted replacing the CLI itself,
use the independent runner retained with the journal:

```
/opt/bob/.venv/bin/python -I /var/lib/bob-upgrades/UPGRADE_ID/runner/runtime_upgrade.py --resume UPGRADE_ID
/opt/bob/.venv/bin/python -I /var/lib/bob-upgrades/UPGRADE_ID/runner/runtime_upgrade.py --rollback UPGRADE_ID
```

The new version is published only after `bob-health` (including each active lane's
core/gateway `/health`) succeeds. Resuming at health/publication restarts units and
rechecks live health. Setup-only oneshots remain manual; newly enabled long-running
services/timers and C5 egress instances start through the journaled activation path.

## Preserved data and hooks

Payload extraction selects only `/opt/bob`, `/usr/local/lib/bob_runtime`,
`/usr/local/libexec/bob`, `/opt/llama`, Bob bin helpers, packaged Bob unit files,
and the memory-slot default. It never extracts rootfs accounts, `/var/lib/bob`,
lane.env, credentials, model weights, database directories or embedder.env.
An edited memory-slot default (including a selected embedder's dimension) is
retained. Env refresh regenerates roles with missing defaults, preserving existing
factor/provider/storage secrets and explicit policy choices; lane.env is unchanged.

The C5 hook registry adds the Builder profile without reprovisioning lane data.
If S1b's `bob-egress-proxy@.service` is packaged, it provisions only that role's
dedicated, journal-reserved UID, merges the bounded-proxy profile/config, writes
the two egress drop-ins and activates the proxy/firewall for active lanes. This
matches the inspected S1b UID/port/unit contract; it never falls back to unrestricted
egress. Rollback restores worker profiles/drop-ins and prior firewall units.
New egress identities remain reserved/inactive after rollback; UIDs are never
recycled. Retained/disabled lanes get configuration refresh but are not started.

OS packages, `/opt/python`, MCP/browser binaries and unrelated runtime dependencies
are outside this payload. A release needing a missing Python interpreter or a
changed root layout is refused before stopping services and needs an explicit
dependency migration. Kernel/systemd, real Postgres and disposable C4-to-C5 upgrade
acceptance must run on Linux/WSL before promotion; offline tests do not prove them.


## CC1 Claude Code pin and payload (2026-11-18)

The image installs the unmodified official glibc linux-x64 Claude Code 2.1.327
binary root:root, mode 0755, at /usr/local/bin/claude using fetch-checked.sh.
The exact URL, SHA-256, 372,904,464 binary bytes and proprietary-license note
are in deploy/runtime/cli-pins.json and the Containerfile build arguments.
This binary is now an explicit runtime-payload root, so upgrades and rollback
include it. Codex's existing pin and binary upgrade coverage are unchanged.
Claude is not covered by Bob's source license: see the linked Anthropic product
preinstallation conditions and applicable Commercial/Consumer Terms.

Every worker/setup launch disables the auto-updater and nonessential traffic.
Subscription setup never imports Windows configuration or API-key credentials.
Fresh provisioning and the idempotent cc1-claude-setup upgrade hook create the
private lane config, fixed profile and worker mount; core receives only sanitized
root-published enrollment metadata. No Claude turn adapter ships in CC1.

The binary adds 355.63 MiB before image/layer/tar overhead. Image and compressed
archive growth must be measured on comparable baseline/candidate builds; it was
not measured in the Windows sandbox. Use the same source epoch, base image and
build options. For an uncommitted candidate, set BOB_RUNTIME_WORKTREE=1 before
build.sh (its default builds HEAD and would omit the changes). Compare docker
image inspect --format '{{.Size}}' IMAGE and stat -c %s on both exported .tar.gz
files. Full operator commands and live acceptance are in CC1-REPORT.md.
