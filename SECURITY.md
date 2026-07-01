# Security

BoB is **v0.96** and runs as a **single-operator** application on a machine you
control, **loopback by default**. The gateway can be exposed for remote access
**behind a TLS-terminating reverse proxy** once you complete the checklist below;
`core` and the infrastructure stay loopback-only. Read this first.

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
- The admin password is stored as a **bcrypt hash** (`BOBCLAW_PASSWORD_HASH`);
  `gen_secrets.py` prints the plaintext once and never writes it to disk. A legacy
  plaintext `BOBCLAW_PASSWORD` is still honored for backward compatibility.
- `.secrets/bobclaw.env` still holds your provider API keys and `BOBCLAW_SECRET` in
  plaintext — it is the highest-value file in the install. Keep it readable only by
  you (restrict its file permissions on any shared machine).
- BoB never bundles or transmits your provider credentials anywhere except directly
  to the provider you configured. See `COMPLIANCE.md`.

## Authentication hardening

- **Login lockout:** after `LOGIN_MAX_FAILURES` (default 5) consecutive failed logins,
  an IP is locked out of `/auth/login` with exponential backoff (`429` + `Retry-After`),
  persisted across restarts; a successful login resets it.
- **Refresh tokens** are opaque server-side rows (not JWTs), so revocation is real.
  Rotation-on-use has a shortened sliding TTL (`REFRESH_TOKEN_DAYS`, default 30) **and**
  an absolute rotation-chain cap (`REFRESH_TOKEN_ABSOLUTE_DAYS`, default 90) — a stolen
  token cannot be rotated forever to extend its life. `POST /auth/revoke-all` (admin)
  kills every session at once.
- **TOTP** is required to start the gateway once configured (startup config validation),
  with RFC 6238 §5.2 replay protection.
- **Security headers** — `Content-Security-Policy`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` — are set on every response,
  including the static web UI.

## Before you expose BoB to a network

v0.96 is single-operator and loopback by default. Before you place the gateway
behind a reverse proxy reachable from a broader network, complete these:

- [ ] Restrict `.secrets/bobclaw.env` file permissions (it holds all your API keys).
- [ ] Terminate TLS at the proxy and forward **only** to the gateway — never expose
      core, the pipeline, Postgres, Redis, or Qdrant.
- [ ] Note the **preview web UI** keeps its session tokens in browser `localStorage`,
      so an XSS in the UI would hand over the session. Prefer an **SSH tunnel** or the
      **native client** for remote access; keep the browser UI on trusted networks.
- [ ] Set a strong, non-default `BOBCLAW_PASSWORD_HASH` and `TOTP_SECRET`.
- [ ] Tighten the Content-Security-Policy in `bobclaw-gateway/security_headers.py`
      for your deployment (e.g. nonces/hashes instead of `'unsafe-inline'` styles).
- [ ] Review rate limiting — the built-in limiter is **per-process / in-memory**, so a
      multi-worker deployment multiplies the effective limit. A Redis-backed limiter is
      the tracked replacement for shared abuse prevention.
- [ ] Consider shortening the access-token lifetime and `REFRESH_TOKEN_ABSOLUTE_DAYS`.

## Scope of v0.96

BoB is **single-operator** and loopback by default. Exposing the **gateway** for
remote access is supported **behind a TLS-terminating reverse proxy** with the
checklist above completed — suitable for trusted personal/remote access, **not** as
a hardened multi-tenant public service. `core`, the pipeline, and the datastores
stay loopback-only. Multi-tenant auth, a shared (Redis-backed) rate limiter,
gateway-only container topology, and mutual TLS are tracked for later releases.

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Open a
GitHub **security advisory** on the repository (Security → Report a vulnerability),
and we will coordinate a fix and disclosure.
