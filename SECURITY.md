# Security

BoB is **v0.95** and is designed to run as a **single-operator, loopback-only**
application on a machine you control. Read this before exposing any part of it to
a network.

## The one rule: don't expose `core`

`bobclaw-core` (port 7825) **has no authentication of its own.** It trusts the
gateway: the gateway authenticates the user (JWT + optional TOTP) and forwards an
HMAC-signed *scope vouch* (keyed by `BOBCLAW_SECRET`) that core verifies. Anything
that can reach core's port directly can dispatch agents, run tools, and (if enabled)
apply edits — with no login.

Therefore:

- **Core binds `127.0.0.1` by default** (`BOBCLAW_CORE_HOST`). Keep it there.
- The **gateway** (port 7826) is the only service meant to face a client, and even
  it defaults to `127.0.0.1`. If you need to reach BoB from another device, put a
  TLS-terminating **reverse proxy** or an **SSH tunnel** in front of the *gateway*
  only — never publish core, the pipeline, Postgres, Redis, or Qdrant.
- `BOBCLAW_SECRET` must be the **same value** for core and gateway (the setup flow
  generates one and writes it once). If it is empty, the vouch is empty and core
  fails closed — it will not honor any scope.

## Loopback-by-default infrastructure

`docker-compose.yml` binds every infrastructure port to `127.0.0.1`
(Postgres `5432`, Redis `6379`, Qdrant `6353/6354`, Playwright `3100`). Nothing is
reachable from another host out of the box. BoB's own services run as host
processes and connect to these over loopback.

- **Postgres password** is read from `POSTGRES_PASSWORD` (the setup flow generates
  a strong value). The committed default (`bobclaw`) exists only so a fresh
  loopback-only checkout starts; **generate a real password before any non-loopback
  or shared use.**

## The build/verify sandbox runs untrusted code

BoB's build pipeline executes **LLM-written code** in its verify gate. That code is
run inside a locked-down Docker container (`BUILD_SANDBOX=docker`, or `auto` which
uses Docker when available): only the per-turn workspace is mounted, `--network
none`, resource caps, ephemeral. This is a **safety feature** — do not set
`BUILD_SANDBOX=subprocess` (host execution) unless you fully trust the model output
and understand the risk. If Docker is unavailable in a context that would run
generated code, BoB is designed to fail closed rather than run it on the host.

## Secrets

- Real secrets live in `.secrets/bobclaw.env`, which is **git-ignored**. Only
  `.secrets/bobclaw.env.example` (placeholders) is tracked.
- BoB never bundles or transmits your provider credentials anywhere except directly
  to the provider you configured. See `COMPLIANCE.md`.

## Scope of v0.95

The loopback single-operator model above is the supported deployment for v0.95.
Full network isolation (gateway-only container topology, mutual TLS, multi-tenant
auth hardening) is tracked for later releases. Do not treat v0.95 as
internet-exposable.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Open a
GitHub **security advisory** on the repository (Security → Report a vulnerability),
and we will coordinate a fix and disclosure.
