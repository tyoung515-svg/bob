---
gist: gist-0002-sample-capability
receiver: bobclaw
commit: 4a27874
status: landed
---

Landing evidence — gist-0002-sample-capability → bobclaw:

| criterion / invariant | re-expressed check | result | tag |
|---|---|---|---|
| INV: disabled ⇒ byte-identical | off-path equality test | pass | PV |
| INV: additive, never reorders | ordering test | pass | XX |
| A1: off ⇒ byte-identical output | unit test | pass | PV |

Status: landed
