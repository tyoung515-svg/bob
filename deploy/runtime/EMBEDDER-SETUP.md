# Packaged embedder setup

Bob owns the llama-server on `127.0.0.1:58081`. The `lmstudio` backend name in
memory slots selects the OpenAI-compatible protocol; no LM Studio or Ollama app
is needed.

The default is Qwen3-Embedding-4B Q4_K_M (2560 dimensions, about 2.4 GB).
The smaller option is **official Qwen3-Embedding-0.6B Q8_0**, not an unofficial
Q4 conversion (1024 dimensions, 639 MB). The official file's SHA256 was inspected
in its [Hugging Face pointer metadata](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/blob/main/Qwen3-Embedding-0.6B-Q8_0.gguf)
on 2026-11-05. Model bytes were not downloaded during implementation. Every
runtime download still passes through `fetch-checked` and is hashed before use.
`configure_build` rejects missing/placeholder digests and non-Qwen URLs.

As root inside the runtime:

```sh
bob-embedder fetch                 # fetch/reverify the current selection
bob-embedder fetch --model 0.6b     # install, verify, select, start, await health
bob-embedder status                # JSON, including a fresh SHA256 check
bob-embedder select --model 4b     # select an already downloaded model
bob-embedder settings embedder.status
bob-embedder settings embedder.install --model 0.6b
```

The two settings operations are also admitted by the existing settings-control
socket, only for its mapped gateway UID. The receipt-only socket cannot install.
`embedder.install {model}` queues the fixed `bob-embedder-install@MODEL.service`
and returns immediately. `embedder.status` returns durable `setup` progress:
`queued`, `downloading`, `verified`, `starting`, `ready`, or `failed`. It reports a
stopped job as failed rather than leaving the UI spinning indefinitely. Polling
uses a root-owned verification receipt bound to file identity, size, mtime and
ctime, avoiding repeated multi-GB hashing. Explicit CLI `status` rehashes bytes.

Desktop first-run and Settings use fixed native commands, guarded by the same
This PC/lane check as `bob-model`, to call these root helper operations over WSL.
This works before first sign-in. It does not forward model-authored commands,
URLs, paths or service names. Remote-server setup through the desktop broker is
not added; run the CLI on that runtime. Existing server deployments are unchanged
unless their slot config opts into `embedder_selection`.

`/etc/bob/embedder.env` is the root-owned selection and contains id, URL, SHA256,
dimension, alias and path. Both embedding slots read this selection. The service
uses its EnvironmentFile; a generated systemd drop-in contains the literal model
file condition because systemd conditions do not expand environment variables.
An upgrade that copies these units onto an old runtime must re-run `fetch` to
publish the complete env and condition. New images initialize these at build time.

Switching is global to this runtime. The operator holds the lane-allocation lock,
stops currently active lane cores, refuses any Qdrant collection with points,
publishes the selection, reloads/restarts the embedder and restores those cores.
Unavailable/malformed vector state also refuses switching. Same-model repair is
allowed. A failed publication attempts to restore the previous selection.
Automatic re-embedding is **not implemented**; keep the existing model when
vectors exist. Do not delete collections to bypass this refusal.

Skipping setup does not download weights or change selection. Recall falls back
to lane-local keyword search when the embedder is unavailable. The opt-in path
reads facts and, when T0 recall is enabled, only the assistant response surface
used by `project_verbatim`. Unknown provider filters fail closed. Candidate
materialization is limited to 200 recent matches per source and 32K characters
per T0 response; lexical recall has lower coverage than semantic retrieval.
The app keeps a visible degraded-memory warning with a Settings shortcut.

RAM recommendations use effective Linux/WSL MemTotal (under 12 GiB suggests the
small model). WSL limits may be lower than physical PC RAM. Both options remain
user-selectable. RAM guidance in the UI is an estimate, not an allocation promise.

Live Linux/systemd, ownership/socket, actual GGUF and 1024/2560-vector acceptance
remain release gates. `tests/embedder_acceptance.py` accepts either selected model
and does not install/download weights itself.
