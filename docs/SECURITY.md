# Security

BoBClaw is a private deployment. Its default startup posture keeps services on the local machine.

## Loopback By Default

- Core binds `127.0.0.1:7825`.
- Gateway binds `127.0.0.1:7826`.
- Claude pipeline binds `127.0.0.1:7823`.
- Compose publishes Postgres, Redis, and Qdrant host ports on `127.0.0.1` only.

Core, the Claude pipeline, and datastores must never be exposed on `0.0.0.0`.

## Gateway Exposure

Only the authenticated gateway may be intentionally exposed. Put a TLS-terminating reverse proxy or SSH tunnel in front of `127.0.0.1:7826`; never publish core, the pipeline, or a datastore directly.

To expose the gateway deliberately, set `BOBCLAW_GATEWAY_HOST=0.0.0.0` only behind that TLS/SSH boundary. Core (`BOBCLAW_CORE_HOST`) and the pipeline (`HOST`) keep their loopback defaults; overriding either is discouraged.

## Database Password

`POSTGRES_PASSWORD` comes from `.secrets/bobclaw.env`. Compose reads it with:

```powershell
docker compose --env-file .secrets/bobclaw.env -f docker-compose.yml up -d
```

There is no shipped default. Compose fails closed when `POSTGRES_PASSWORD` is unset.

Public-repository hardening is tracked separately and deferred to R7. See [docs/STARTUP.md](STARTUP.md) for local startup.
