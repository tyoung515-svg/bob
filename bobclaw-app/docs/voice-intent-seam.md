# Voice intent → action seam (U11)

**Status:** seam only — **no STT/TTS engine is wired in v1** (SPEC-UI-OVERHAUL §7). This document is
the deliverable: it maps every U3 action-registry action to the natural-language **voice intent** a
user would speak to invoke it. When a speech engine lands, the voice frontend matches an utterance to
an action `id` below, then runs it through the **same D11-tier / D12-guardrail path** the Ask-Bob
helper bubble already uses (`dispositionFor` in `ui/components/AskBobLogic.kt`). Voice adds **no new
execution path** — it is the third frontend over the one registry (SPEC §3 / Decision D4: *"one
registry, three frontends — palette, helper bubble, voice"*).

## Where this lives in code

- **Registry (source of truth):** core `core/actions/registry.py` → `SEED_ACTIONS`, served through the
  gateway `GET /capabilities` `actions[]` payload (each entry: `{id, title, description_plain,
  params_schema, risk, undo_hint, page_scope, binding}`).
- **App-side seam constant:** `ui/components/VoiceAffordances.kt` → `VOICE_INTENTS` (this table, as a
  map) + `SEED_ACTION_IDS`. `VoiceAffordancesTest` asserts **every** seed action id has a non-blank
  intent (`missingVoiceIntents()` must be empty) — so this doc can never silently drift out of sync.
- **Gating:** `voiceAffordancesVisible(voiceBeta)` — the affordances render only behind the
  `voice_beta` preview flag; OFF (default) ⇒ the UI is byte-identical to today.

## Intent map — every registry action

Risk / binding columns are reproduced from the U3 registry (RESULTS-U3) so the routing is explicit:
a `gated` intent is **never** auto-executed by voice — it surfaces to Approvals for a human, exactly
as the bubble does.

| # | action `id` | voice intent (what the user says) | risk (D11) | binding (real op) | routing when spoken |
|---|-------------|-----------------------------------|------------|-------------------|---------------------|
| 1 | `create_team` | "Bob, create a team called \<name\>" | reversible | POST `/teams` | confirm-once, then execute (D12) |
| 2 | `delete_team` | "Bob, delete the \<name\> team" | reversible | DELETE `/teams/{name}` | confirm-once, then execute; undo = session-cached YAML restore |
| 3 | `pin_face` | "Bob, use the \<face\> face" | reversible | WS `switch_face` | confirm-once, then execute; undo = clear/re-pin |
| 4 | `switch_profile` | "Bob, switch to the \<profile\> profile" | reversible | WS `switch_profile` | confirm-once, then execute; undo = switch back |
| 5 | `forget_fact` | "Bob, forget that \<fact\>" | **gated** | DELETE `/memory/facts/{fact_id}` | **routes to Approvals** — never auto-fired |
| 6 | `new_conversation` | "Bob, start a new conversation" | reversible | POST `/conversations` | confirm-once, then execute; undo = delete conversation |
| 7 | `approve` | "Bob, approve that" | **gated** | POST `/approvals/{approval_id}/decide` `{decision: approve}` | **routes to Approvals** — a human decides |
| 8 | `deny` | "Bob, deny that" | **gated** | POST `/approvals/{approval_id}/decide` `{decision: reject}` | **routes to Approvals** — a human decides |

**Coverage: 8 / 8 registry seed actions mapped.** (Asserted by
`VoiceAffordancesTest.every_seed_action_has_a_voice_intent`.)

## How an utterance becomes an action (future engine)

1. STT transcribes the utterance.
2. Intent matcher resolves it to an action `id` from this seam (utterance ≈ intent phrase; slots like
   `<name>` fill `params_schema` fields).
3. The registry entry's `page_scope` scopes it (voice honours the same page-scoping the bubble uses via
   `actionsForPage`).
4. `dispositionFor(action, confirmedActions, mutatingThisTurn)` decides: `read`/confirmed-`reversible`
   → execute; first-use `reversible` → confirm-once; `gated` (or unknown) → **route to Approvals**;
   over the per-turn rate cap → refuse. A consequence toast (`consequenceToast`) narrates it.
5. `read aloud` (TTS) speaks Bob's reply back — the per-message placeholder in `ChatThread.kt` is the
   surface it will attach to.

No step here is app-page-specific: adding a new action to the core registry lights it up for voice
(and the palette, and the bubble) with only one new row in `VOICE_INTENTS` + this table.
