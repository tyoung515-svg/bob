package com.bobclaw.ui.components

/**
 * Pure, Compose-free logic for the U11 voice affordances (SPEC-UI-OVERHAUL §7 / §3, Decision D4).
 *
 * This is the **load-bearing fence** for U11: every gating decision lives here so `commonTest` can
 * prove the two invariants with no UI —
 *   1. **Flag OFF ⇒ nothing new renders** ([voiceAffordancesVisible] returns `false` for the
 *      default/off flag, so the guarded `if (...) { mic }` blocks in the composer / bubble / message
 *      row emit ZERO extra Composables ⇒ the UI is byte-identical to today), and
 *   2. the **intent→action seam** ([VOICE_INTENTS]) names EVERY U3 registry seed action
 *      ([SEED_ACTION_IDS]) — the same completeness the `docs/voice-intent-seam.md` doc asserts.
 *
 * NO speech engine is wired in v1 (SPEC §7: "NO STT/TTS engine in v1; the seam is the deliverable").
 * The affordances are therefore rendered inert-but-present: the mic is DISABLED with a "coming soon"
 * tooltip ([micEnabled] is `false` while [SPEECH_ENGINE_PRESENT] is `false`), and read-aloud is a
 * no-op placeholder. When a real engine lands, flip [SPEECH_ENGINE_PRESENT] and wire the callbacks —
 * no page/composer change needed (the voice lane plugs into the registry, §3).
 */

/**
 * Whether the voice affordances render at all. This is the ONE predicate every U11 conditional
 * branches on: `false` (the [com.bobclaw.network.UserPrefs.voiceBeta] default) ⇒ the composer /
 * bubble / message row render exactly as they do today (no mic button, no read-aloud row emitted).
 */
fun voiceAffordancesVisible(voiceBeta: Boolean): Boolean = voiceBeta

/**
 * Whether a real speech engine (STT/TTS) is wired. **`false` in v1** — the seam is the deliverable,
 * not the engine (SPEC §7). Kept as a single named constant so the "no engine ⇒ disabled + tooltip"
 * behaviour is derived, never hardcoded at each call site; flipping this lights the affordances up.
 */
const val SPEECH_ENGINE_PRESENT: Boolean = false

/**
 * Whether the mic button may be pressed: only when a speech engine is present. With no engine it is
 * rendered DISABLED with the [MIC_TOOLTIP_RES]-keyed "coming soon" tooltip (SPEC §7).
 */
fun micEnabled(enginePresent: Boolean = SPEECH_ENGINE_PRESENT): Boolean = enginePresent

/**
 * Whether the per-message "read aloud" control does anything yet: only with a speech engine. Without
 * one it is a visible placeholder that performs no action (SPEC §7 "read-aloud is a placeholder that
 * does nothing yet").
 */
fun readAloudEnabled(enginePresent: Boolean = SPEECH_ENGINE_PRESENT): Boolean = enginePresent

// ── intent → action seam (SPEC §3 · D4: "one registry, three frontends — palette, bubble, voice") ──
/**
 * The U3 registry seed action ids, verbatim from core `core/actions/registry.py::SEED_ACTIONS`
 * (read at authoring time to be exhaustive — cited in RESULTS-U11 verify #2). The live registry is
 * served through `GET /capabilities` `actions[]`; this constant is the seam's completeness anchor so
 * a test (and the seam doc) can assert every action has a voice intent WITHOUT a running gateway.
 */
val SEED_ACTION_IDS: List<String> = listOf(
    "create_team",
    "delete_team",
    "pin_face",
    "switch_profile",
    "forget_fact",
    "new_conversation",
    "approve",
    "deny",
)

/**
 * The intent→action seam: each U3 action id → a natural-language voice intent phrase a user would
 * say to invoke it. This is the U11 deliverable's machine-readable core (the prose lives in
 * `docs/voice-intent-seam.md`, which mirrors this map). The voice frontend, once an STT engine lands,
 * matches an utterance to an id here, then runs it through the SAME D11-tier / D12-guardrail path the
 * Ask-Bob bubble already uses ([dispositionFor]) — voice adds NO new execution path.
 */
val VOICE_INTENTS: Map<String, String> = linkedMapOf(
    "create_team" to "Bob, create a team called <name>",
    "delete_team" to "Bob, delete the <name> team",
    "pin_face" to "Bob, use the <face> face",
    "switch_profile" to "Bob, switch to the <profile> profile",
    "forget_fact" to "Bob, forget that <fact>",
    "new_conversation" to "Bob, start a new conversation",
    "approve" to "Bob, approve that",
    "deny" to "Bob, deny that",
)

/** The voice intent phrase for [actionId], or `null` if the id has no mapped intent. */
fun voiceIntentFor(actionId: String): String? = VOICE_INTENTS[actionId]

/**
 * The seed action ids that have NO voice intent mapping — the seam-doc completeness check. MUST be
 * empty (asserted by `VoiceAffordancesTest`): every registry action names a voice intent (SPEC §7).
 */
fun missingVoiceIntents(actionIds: List<String> = SEED_ACTION_IDS): List<String> =
    actionIds.filter { it !in VOICE_INTENTS }
