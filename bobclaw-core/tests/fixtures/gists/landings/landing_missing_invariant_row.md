---
gist: gist-0002-sample-capability
receiver: bobclaw
commit: 4a27874
status: landed
---

Landing evidence — gist-0002-sample-capability → bobclaw:

Violation fixture: gist-0002 declares two invariants but this landing has only one
INV row, so an invariant lacks its mandatory §7 evidence row.

| criterion / invariant | re-expressed check | result | tag |
|---|---|---|---|
| INV: disabled ⇒ byte-identical | off-path equality test | pass | PV |
| A1: off ⇒ byte-identical output | unit test | pass | PV |

Status: landed
