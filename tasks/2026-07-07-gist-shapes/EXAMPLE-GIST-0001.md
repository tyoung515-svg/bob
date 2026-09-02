---
id: gist-0001-spawn-identity-card
title: Faces know they run inside BoB (spawn-identity card)
criticality: capability
verification_class: spec-conformance
transfer: shaped
distribution: public
intended: [bobclaw, enterprise, users]
provenance:
  source_tree: bob
  commit: 958d01a
  release: v0.97.0
  results: "bob CHANGELOG.md §[0.97.0] + README (docs commit 2be1d77)"
requires:
  capabilities: [per-turn-system-prompt-assembly, env-config-surface]
  gists: []
  baseline: ">=0.96"
facts:
  code_default: disabled
status: shaped
# Retroactive worked example: this capability transferred bob → bobclaw on 2026-07-05
# (bobclaw commit 4a27874) by hand-carried re-derivation — i.e., it traveled as a shape
# before the standard existed. Landing evidence: GIST-SHAPES.md §7 (bobclaw) + bob native.
---

## Intent

A face asked "what are you / where are you running?" should answer with its platform identity —
"I'm BoB's General Assistant, served by …" — instead of "I have no idea I'm deployed." Matters
for user trust (the product feels like one system, not a bare model wearing a UI) and for
support/debugging (any turn can self-report which face and backend produced it).

## Contract

- When enabled, every executed turn carries a **front-most system card** naming three things:
  the platform (BoB), the face serving the turn (name + role), and the backend serving it.
- An **enable flag** controls it; the **code default is disabled** (see `facts`). Reference name
  `BOB_IDENTITY_ENABLED` — renameable per tree.
- An **override variable** replaces the card text wholesale, for operators who want their own
  wording. Reference name `BOB_IDENTITY_TEXT` — renameable per tree.
- The card states the *resolved* face/backend for the turn (post-routing truth), not the
  requested one.

## Invariants

1. **Disabled ⇒ byte-identical**: with the flag off, prompt assembly is byte-for-byte what it
   was before the capability existed. (No-regression is the contract, not a nicety.)
2. **Front-most, additive**: the card *prepends* the existing system-prompt stack; it never
   replaces or reorders it.

## Acceptance

1. Flag off ⇒ prompts byte-identical to pre-feature assembly.
2. Flag on ⇒ the first system content of a turn names platform + face (name/role) + backend.
3. Override set ⇒ card text equals the override exactly.
4. *(optional)* Live smoke: a face asked "are you deployed inside something?" with the flag on
   answers with BoB identity.
5. Full-suite no-regression on the receiving tree.

## Scope fence

- One global card template v1 — no per-face custom cards.
- The card does not alter routing, face selection, or escalation in any way.
- Not a substitute for face system prompts — identity, not persona.
- Fan-out worker sub-turn inheritance is out of scope v1 (mirrors the source tree's own
  deferral of sub-turn locale inheritance).

## Adaptation notes

- **Injection point differs per tree**: the source tree splices the card after its locale card;
  a tree without a locale splice lands it adjacent to whatever per-turn context splice it has
  (bobclaw: after the project-context splice).
- **Shipped default is per-tree policy, not contract**: the source tree's shipped `.env`
  deliberately opts IN while its code default stays off; bobclaw kept both off. Receivers
  choose their own shipped posture.
