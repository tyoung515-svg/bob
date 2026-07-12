# UPGRADE — tag-to-tag runbook

The supported upgrade path is **pinned tag → pinned tag** (never tracking `main`).
The sequence is always the same; per-version migration notes are below.

## The runbook

1. **Read the target tag's `CHANGELOG.md` entry** — including *Known limitations* —
   and the migration notes below for every version you are crossing.
2. **Stop the stack:** `./scripts/win/stop-all.ps1` (Docker containers keep running;
   that's fine — the datastores are upgraded by compose, not by git).
3. **Check out the target tag:**
   ```powershell
   git fetch --tags
   git checkout vX.Y.Z
   ```
4. **Re-sync dependencies from the pinned locks** (idempotent):
   ```powershell
   uv pip install --python .venv\Scripts\python.exe -r bobclaw-core\requirements.lock
   uv pip install --python .venv\Scripts\python.exe -r bobclaw-gateway\requirements.lock
   uv pip install --python .venv\Scripts\python.exe -r bobclaw-claude-pipeline\requirements.lock
   ```
5. **Apply the migration notes** for the versions you crossed (below).
6. **Verify:** `pwsh ./run_baseline_tests.ps1` — must end `BASELINE GREEN`.
7. **Bring it up + smoke:** `./scripts/win/start-local.ps1`, then
   `./scripts/win/status.ps1` (everything OK), log in, and run one chat turn.
8. **Rollback** (if anything above fails): `git checkout <previous tag>` and re-run
   steps 4–7. Volumes/data are not touched by a checkout.

## 0.97.0 → 0.98.0

### 1. Compose project name (do this BEFORE any `docker compose up`)

v0.98 pins the compose project name (default `bobclaw`) instead of deriving it from
your checkout folder name. If your checkout folder was NOT named `bobclaw`, compose
would start a fresh project — new empty volumes — and your conversations/memory
would *look* gone (they aren't; the old volumes still exist under the old prefix).

Find your existing project prefix and pin it in `.secrets\bobclaw.env`:

```powershell
docker volume ls --format "{{.Name}}" | Select-String pgdata
# e.g. "bob_pgdata"  → your old project name is "bob"
```

Add to `.secrets\bobclaw.env`:

```
COMPOSE_PROJECT_NAME=bob     # your old project name from the volume prefix
```

(Fresh v0.98 installs skip this — they just get the pinned `bobclaw` project.
Running two installs on one host: give each a distinct `COMPOSE_PROJECT_NAME`
**and** distinct datastore host ports — `BOBCLAW_PG_PORT` / `BOBCLAW_REDIS_PORT` /
`BOBCLAW_QDRANT_PORT` / `BOBCLAW_QDRANT_GRPC_PORT`, with matching `POSTGRES_URL` /
`REDIS_URL` / `MEMORY_QDRANT_URL`. Defaults are unchanged: 5432 / 6379 / 6353 / 6354.)

### 2. Default embedder changed (memory users only)

The `embed_text` slot default moved from `granite-embedding-311m` (768-dim) to
`qwen3-embedding-4b` (2560-dim, last-token pooling). Embeddings from different
models/dims never mix — the fingerprint guard fail-closes writes on mismatch —
so an existing memory store needs one of:

- **Keep granite** (no re-index): in `bobclaw-core/config/memory_slots.toml`,
  restore the commented granite block for `[slot.embed_text]`, and launch the
  embedder with `BOBCLAW_EMBED_POOLING=mean`. Everything keeps working as on 0.97.
- **Move to the 4B default** (recommended; better measured recall): serve a
  `qwen3-embedding-4b` GGUF (`BOBCLAW_EMBED_GGUF=...`, pooling `last` is the
  launcher default) and re-index your facts into the new 2560-dim collection
  (memory re-extracts going forward; historical facts re-embed on reindex).
  The old 768-dim collection is left untouched until you delete it.

If memory was never enabled (`MEMORY_ENABLED=false`, the default), skip this.

### 3. Federation ledger example (`repo` field)

If you copied `bobclaw-core/data/ledger_instances.example.json` on 0.97: the
BoB-owned instance (`bobclaw-memory`) must carry `"repo": "."` — the write fence
uses it as the ownership signature at registration. Instances without it are
treated as foreign (read-only). The current example is correct; older copies may
lack the field.

### 4. Optional zero-Docker provider

Only if you want the experimental zvec path: `uv pip install --python
.venv\Scripts\python.exe zvec==0.5.1`, then opt in via
`bobclaw-core/config/memory_stores.toml` (see the commented block). Qdrant
remains the default and the recommendation for v0.98 (see the CHANGELOG's known
limitations for the measured ANN recall gap).

### 5. Extractor version bump (memory users only)

L1 fact dedup identity changed (extractor v1 → v2): the first re-encounter of an
already-known fact after upgrade may extract once more, then dedup as normal. No
action needed; noted so a small one-time fact-count bump doesn't surprise you.
