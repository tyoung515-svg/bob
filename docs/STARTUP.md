# BoBClaw Startup

BoBClaw is a private multi-agent orchestration platform: core runs on `127.0.0.1:7825`, the authenticated gateway on `127.0.0.1:7826`, the Claude build pipeline on `127.0.0.1:7823`, and the KMM desktop app lives in `bobclaw-app/`.

## Windows Start

1. Start Docker Desktop.
2. Create the root virtual environment at `.venv`.
3. Copy `.secrets\bobclaw.env.example` to `.secrets\bobclaw.env` and set a strong `POSTGRES_PASSWORD`.
4. From the repo root, run:

```powershell
scripts\win\start-all.ps1
```

| Service | Address |
| --- | --- |
| Core | `127.0.0.1:7825` |
| Gateway | `127.0.0.1:7826` |
| Claude pipeline | `127.0.0.1:7823` |
| Postgres | `127.0.0.1:5432` |
| Redis | `127.0.0.1:6379` |
| Qdrant | `127.0.0.1:6353` |

**Network posture:** The stack binds loopback (`127.0.0.1`) by default. Only the authenticated gateway may be intentionally exposed; see [docs/SECURITY.md](SECURITY.md).
