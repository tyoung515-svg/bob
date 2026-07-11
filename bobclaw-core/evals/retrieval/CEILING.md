# L-EVAL Retrieval Ceiling

Measured on **2026-07-12** from the local LM Studio endpoint `http://127.0.0.1:1234` on this machine.

## Measurement

- Corpus: 257 deterministic chunks from 115 repository-local sources.
- Eval set: 100 query-to-single-relevant-chunk rows.
- Sources: tracked root technical Markdown, comments from `.secrets/bobclaw.env.example`, and application module docstrings under `bobclaw-core`; tests and eval artifacts are excluded.
- Chunk target: approximately 300-800 characters; actual corpus range is 76-790 because a few standalone module docstrings are shorter than the target.
- Document vectors are embedded once in batches of 32.
- Each query is sent as one OpenAI-compatible `POST /v1/embeddings` request with `input` as a one-item list and the selected `model`.
- Query and document calls use separate template seams (`embed_query` and `embed_doc`). No instruction templates are configured for this run, so both templates were `null` and raw text was sent.
- Retrieval is exact brute-force cosine over NumPy-normalized `float32` vectors. This is an embedding ceiling, not a Qdrant or ANN measurement.
- p95 is over per-query **query embedding plus NumPy search**. One-time document embedding is reported separately and is not included in p95.
- One untimed warmup query ran after document indexing and before the 100 timed queries.

## Results

| Model | Output dim | Document embed time | Recall@10 | p95 embed+search |
| --- | ---: | ---: | ---: | ---: |
| `text-embedding-qwen3-embedding-4b@q4_k_m` | 2560 | 14,845.445 ms | 95/100 = **95.0%** | **42.752 ms** |
| `text-embedding-qwen3-embedding-0.6b@q8_0` | 1024 | 10,606.925 ms | 88/100 = **88.0%** | **25.899 ms** |

Query latency distributions:

- 4B: min 25.794 ms, median 27.319 ms, p95 42.752 ms, max 51.373 ms.
- 0.6B: min 17.355 ms, median 19.972 ms, p95 25.899 ms, max 27.146 ms.

The 4B model ID above is the requested `q4_k_m` serving model. "Full-precision ceiling" here means full brute-force cosine over the returned float vectors; it does not claim that the served model weights are f16.

## Ratcheted thresholds

- Recall threshold: at least 90% of the measured 4B ceiling.
  - `0.90 x 0.950 = 0.855`, or **85.5% recall@10**.
  - On this 100-row set, that means at least **86/100** hits.
  - The 0.6B run measured **88/100**, so it clears this recall ratchet on this eval set.
- p95 budget: approximately **200 ms** for embed plus search.
  - 4B measured **42.752 ms**, 157.248 ms below the budget.
  - 0.6B measured **25.899 ms**, 174.101 ms below the budget.

No endpoint stall, model-load failure, timeout, or response-shape failure occurred in either requested run.

## Reproduction

```powershell
.venv\Scripts\python bobclaw-core\evals\retrieval\harness.py `
  --endpoint http://127.0.0.1:1234 `
  --model text-embedding-qwen3-embedding-4b@q4_k_m

.venv\Scripts\python bobclaw-core\evals\retrieval\harness.py `
  --endpoint http://127.0.0.1:1234 `
  --model text-embedding-qwen3-embedding-0.6b@q8_0
```


