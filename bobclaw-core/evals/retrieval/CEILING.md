# L-EVAL Retrieval Ceiling

Measured on **2026-07-12** from the local LM Studio endpoint `http://127.0.0.1:1234` on this machine.

## Verdict

- `eval_set.jsonl` is a **lexical-overlap embedding smoke**, not a sound release ratchet. The cross-audit found median 62.5% content-token overlap and 76/100 rows above 50% overlap.
- `eval_set_paraphrase.jsonl` is the honest embedding-only ceiling in this lane. It contains 40 author-blind paraphrases selected with random seed `20260712`.
- The paraphrase-set ratchet is **ADVISORY** until L6 runs the same paired set through the production retrieval path via the zvec provider.

## Eval sets

### Lexical-overlap smoke

- 100 original query-to-single-relevant-chunk rows.
- Retained for regression diagnosis and lexical embedding smoke coverage.
- Must not be used as the release-quality retrieval threshold.

### Author-blind paraphrase ceiling

- 40 source pairs selected by `random.Random(20260712).sample(rows, 40)`.
- Each new query was written from its original query only, without consulting the target chunk during authorship.
- The post-write guard tokenizes lowercase `[a-z0-9]+`, removes the harness's English stopword set, and rejects any remaining query token present in the target chunk.
- All 40 rows have zero content-token overlap under that guard.
- `source_query_id` preserves pairing with the lexical row.

## Protocol

- Corpus: 257 deterministic chunks from 115 repository-local sources.
- Sources: tracked root technical Markdown, comments from `.secrets/bobclaw.env.example`, and application module docstrings under `bobclaw-core`; tests and eval artifacts are excluded.
- Chunk target: approximately 300-800 characters; actual range is 76-790 because a few standalone module docstrings are shorter.
- Document vectors are embedded once in batches of 32.
- Query and document calls use separate `embed_query` and `embed_doc` template paths. Both templates were `null`, matching the current slot configuration.
- Retrieval is exact brute-force cosine over normalized NumPy `float32` vectors.
- p95 measures one query embedding request plus NumPy search. One-time document embedding is reported separately.
- One untimed query warmup runs after document embedding.
- Distractors are **kept and scored**: curated near misses are useful only when their strict outrank rate is reported, so `distractor_win=true` when any nominated distractor scores above the target.
- Wilson intervals apply to binomial recall and distractor-win proportions; latency percentiles are point measurements, not binomial estimates.

## Lexical-overlap smoke results

| Model | Recall@10, Wilson 95% CI | Distractor-win rate, Wilson 95% CI | p95 embed+search | Document embed |
| --- | --- | --- | ---: | ---: |
| `text-embedding-qwen3-embedding-4b@q4_k_m` | 95/100 = **95.0%** [88.8%, 97.8%] | 19/100 = **19.0%** [12.5%, 27.8%] | 28.640 ms | 15,645.354 ms |
| `text-embedding-qwen3-embedding-0.6b@q8_0` | 88/100 = **88.0%** [80.2%, 93.0%] | 31/100 = **31.0%** [22.8%, 40.6%] | 25.426 ms | 10,511.509 ms |

Paired hit outcomes over the same 100 queries:

- Both models hit: 86.
- 4B only: 9.
- 0.6B only: 2.
- Neither model hit: 3.

## Author-blind paraphrase results

| Model | Recall@10, Wilson 95% CI | Distractor-win rate, Wilson 95% CI | p95 embed+search | Document embed |
| --- | --- | --- | ---: | ---: |
| `text-embedding-qwen3-embedding-4b@q4_k_m` | 26/40 = **65.0%** [49.5%, 77.9%] | 17/40 = **42.5%** [28.5%, 57.8%] | 29.390 ms | 15,079.805 ms |
| `text-embedding-qwen3-embedding-0.6b@q8_0` | 16/40 = **40.0%** [26.3%, 55.4%] | 24/40 = **60.0%** [44.6%, 73.7%] | 25.670 ms | 10,435.356 ms |

Paired hit outcomes over the same 40 queries:

- Both models hit: 13.
- 4B only: 13.
- 0.6B only: 3.
- Neither model hit: 11.

The 4B model ID is the requested `q4_k_m` serving model. "Full-precision ceiling" here means full brute-force cosine over returned float vectors; it does not claim f16 model weights.

## Precision limits

- At `n=100` and observed recall near 90%, a 95% interval is roughly **plus or minus 6 percentage points**. The exact Wilson intervals above show that this smoke set cannot support a tight release boundary.
- At `n=40`, uncertainty is materially worse. The 4B paraphrase estimate is 65.0% with a 49.5%-77.9% interval, roughly minus 15.5 / plus 12.9 points. The set is useful for direction and paired comparisons, not a hard release gate.

## Advisory ratchet

The honest baseline is the 4B paraphrase result: **65.0% recall@10, Wilson 95% CI [49.5%, 77.9%]**.

- Advisory point threshold: at least 90% of the measured 4B paraphrase ceiling.
- `0.90 x 0.650 = 0.585`, or **58.5% recall@10**.
- On 40 rows the discrete minimum is **24/40 = 60.0%**.
- Every candidate result must be reported with its Wilson 95% CI and paired per-query outcomes.
- This threshold must not block release until the queued L6 production-path run through the zvec provider confirms it.
- The p95 embed-plus-search budget remains approximately **200 ms**. The measured 4B paraphrase p95 is 29.390 ms; store/provider overhead is not represented here.

No endpoint stall, model-load failure, timeout, or response-shape failure occurred in any of the four runs.

## Paired result artifacts

Each JSONL row includes `pair_id`, `eval_id`, `hit_at_10`, full `rank`, `distractor_win`, winning distractor IDs, top ten IDs, and query latency:

- `results_lexical_4b.jsonl`
- `results_lexical_06b.jsonl`
- `results_paraphrase_4b.jsonl`
- `results_paraphrase_06b.jsonl`

## Reproduction

```powershell
.venv\Scripts\python bobclaw-core\evals\retrieval\harness.py `
  --endpoint http://127.0.0.1:1234 `
  --model text-embedding-qwen3-embedding-4b@q4_k_m `
  --eval-set bobclaw-core\evals\retrieval\eval_set_paraphrase.jsonl `
  --results-jsonl bobclaw-core\evals\retrieval\results_paraphrase_4b.jsonl

.venv\Scripts\python bobclaw-core\evals\retrieval\harness.py `
  --endpoint http://127.0.0.1:1234 `
  --model text-embedding-qwen3-embedding-0.6b@q8_0 `
  --eval-set bobclaw-core\evals\retrieval\eval_set_paraphrase.jsonl `
  --results-jsonl bobclaw-core\evals\retrieval\results_paraphrase_06b.jsonl
```

For the lexical-overlap smoke, repeat the two commands with `--eval-set bobclaw-core\evals\retrieval\eval_set.jsonl` and write to `results_lexical_4b.jsonl` / `results_lexical_06b.jsonl` respectively.
