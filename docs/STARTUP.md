# Starting BoB 0.99.1

Use [the install guide](../AGENTS-SETUP.md) for a new installation and
[the upgrade guide](../UPGRADE.md) for an existing one. There is no browser UI.

## Windows desktop installer

Open the installed desktop app. Choose **Set up Bob on this PC**, complete WSL 2
runtime setup, create a lane and enroll its authenticator. Select the lane, sign
in and connect a model. Docker Desktop and the old Windows host-process startup
scripts are not part of this route. The earlier Kotlin app is incompatible with
this release.

The runtime runs core, gateway and bridge per lane. The first available lane
normally uses loopback ports 7835, 7836 and 8902; later lanes get unused ports.
Datastores also stay on loopback. Use the app's setup/recovery and Upgrade controls
for this runtime; do not replace its files with a source checkout.

## Standalone Ubuntu server

After completing either source-install route in AGENTS-SETUP.md:

```bash
sudo systemctl start bob-source-core bob-source-gateway
sudo systemctl status bob-source-core bob-source-gateway --no-pager
curl --fail http://127.0.0.1:7835/health
curl --fail http://127.0.0.1:7836/health
```

Datastores must already be running. Native services are `postgresql`,
`redis-server` and `bob-source-qdrant`. For Docker datastores, from the same
checkout and with the same private environment/project used at installation:

```bash
sudo docker compose --env-file .secrets/bobclaw.env -f docker-compose.yml \
  up -d --wait --wait-timeout 180 postgres redis qdrant
```

Do not erase volumes or rerun the install script over existing state.
Health responses do not establish that a model is enrolled or memory works.

Start the terminal client from the source checkout:

```bash
sudo -u bob-source env PYTHONPATH=/opt/bob-source/bobclaw-tui \
  /opt/bob-source/.venv/bin/python -m bobclaw_tui --gateway 127.0.0.1:7836
```

For the desktop app choose **Connect to a Bob server**, using the SSH account
and lane broker launcher from AGENTS-SETUP.md. The gateway stays on loopback.
Source setup does not provide image-only worker, subscription enrollment or
runtime recovery features. Read [SECURITY.md](../SECURITY.md) before making
services reachable from another device.
