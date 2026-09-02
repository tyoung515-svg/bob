---
gist: gist-0002-sample-capability
receiver: bobclaw
commit: 4a27874
status: landed
---

Landing evidence — gist-0002-sample-capability → bobclaw (commit `4a27874`):

| criterion / invariant | re-expressed check | result | tag |
|---|---|---|---|
| INV: disabled ⇒ byte-identical | off-path equality test | pass | PV |
| INV: additive, never reorders | ordering test | pass | PV |
| A1: off ⇒ byte-identical output | unit test | pass | PV |
| A2: on ⇒ surfaced at first point | unit test | pass | VS |
| Suite no-regression | core delta | pass | PV |

Status: **landed** (all mandatory rows PV/VS).
