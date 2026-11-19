#!/bin/sh
# Prove the packaged llama-server discovers and loads a CPU backend by itself.
# b11747 facts (checked against the real image): `--list-devices` prints no CPU device
# and exits 0 even with no backend, so it proves nothing. Starting the server with
# --verbose against an absent model logs "load_backend: loaded CPU backend from ..."
# during backend discovery and then fails at the model file; without a usable backend
# it logs "no backends are loaded" instead. No model or network is needed.
set -eu
unset GGML_BACKEND_PATH
cd /
probe=/nonexistent/bob-embedder-backend-probe.gguf
output=$(timeout 60 /usr/local/bin/llama-server --verbose -m "$probe" --host 127.0.0.1 --port 1 2>&1) || true
printf '%s\n' "$output" | grep -E 'load_backend|no backends|GGUF' || true
if printf '%s\n' "$output" | grep -q 'no backends are loaded'; then
    echo 'embedder: no llama.cpp backend loaded' >&2
    exit 1
fi
printf '%s\n' "$output" | grep -Eq 'load_backend: loaded CPU backend from /opt/llama/libggml-cpu(-[^ /]+)?\.so([[:space:]]|$)' || {
    echo 'embedder: packaged CPU backend did not load' >&2
    exit 1
}
printf '%s\n' "$output" | grep -q "failed to open GGUF file '$probe'" || {
    echo 'embedder: llama-server did not reach model loading after backend discovery' >&2
    exit 1
}
# Image builds without weights still prove CPU discovery above. An installed
# model additionally requires the selected alias on the running service.
if [ -f /etc/bob/embedder.env ]; then
/opt/bob/.venv/bin/python -I - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/usr/local/lib/bob_runtime')
from embedder import selection, loaded_alias
model = selection()
if Path(model['path']).is_file() and not loaded_alias(model):
    sys.exit('embedder: configured alias absent from /v1/models')
PY
fi
