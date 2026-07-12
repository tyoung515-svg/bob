package com.bobclaw.network

/**
 * User preferences persisted across launches. Platform-specific implementations do the actual IO;
 * commonMain only sees this interface (mirrors the [SessionStore] pattern — no `java.*` here).
 */
interface PrefStore {
    fun load(): UserPrefs
    fun save(prefs: UserPrefs)
}

/**
 * All persisted UI prefs.
 *  - [uiScale] is FUNCTIONAL in lane 4a: an app-wide `LocalDensity` multiplier, clamped 0.8f..1.5f.
 *  - [accentName] is reserved for lane 4b (the user-settable accent picker); persisted now so 4b
 *    just reads it.
 *  - [theme] / [density] are parked stubs (DESIGN §5) — rendered as disabled selectors, not wired.
 */
data class UserPrefs(
    val uiScale: Float = 1.0f,            // 0.8f .. 1.5f, app-wide LocalDensity multiplier
    val accentName: String = "teal",       // reserved for 4b; persisted now so 4b just reads it
    val theme: String = "dark",            // stub: dark | light | system
    val density: String = "comfortable",   // stub: comfortable | compact
    val locale: String = "en",             // i18n: en | zh-Hans | zh-Hant (header toggle)
    // U5 (D12 guardrail): action ids the user has confirmed once for the Ask-Bob helper bubble.
    // A reversible-write action prompts a one-time confirm on FIRST use per id; after that it runs
    // without re-prompting. Persisted so "confirm once" survives relaunch. Empty by default.
    val confirmedActions: Set<String> = emptySet(),
    // U6 (SPEC §6): the Simple/Pro experience knob. `simple` (default) hides jargon and, for the
    // Approvals screen, auto-fetches the plain-language explanation; `pro` shows the technical
    // surface (cc_edit diff inline) and fetches the explanation only on click. U6 adds this pref
    // MINIMALLY and gates ONLY the approvals literacy behavior on it; **U9 owns the full app-wide
    // Simple/Pro calibration sweep and should EXTEND this same field** (chat chips, Teams wording,
    // routing-tab visibility) rather than introduce a parallel one.
    val experienceLevel: String = "simple",
    // U11 (SPEC §7 / §3): the `voice_beta` preview flag. When OFF (the default) the voice
    // affordances (mic buttons in the chat composer + Ask-Bob bubble, and the per-message "read
    // aloud" placeholder) render NOTHING — the UI is byte-identical to today. When ON they render
    // inert-but-present (mic disabled with a "coming soon" tooltip, read-aloud a no-op placeholder):
    // NO speech engine is wired in v1, the seam is the deliverable (see docs/voice-intent-seam.md).
    // Toggled from Settings → Advanced. Default false ⇒ nothing new ships until the user opts in.
    val voiceBeta: Boolean = false,
)

/** Default no-op store (no persistence) — used by callers/platforms that don't wire one. */
object NoopPrefStore : PrefStore {
    override fun load(): UserPrefs = UserPrefs()
    override fun save(prefs: UserPrefs) {}
}

/** Inclusive bounds for the functional UI-scale control (DESIGN §5). */
const val UI_SCALE_MIN = 0.8f
const val UI_SCALE_MAX = 1.5f

/** The valid [UserPrefs.theme] values (A1: the Dark/Light/System toggle). Anything else → default. */
val THEME_OPTIONS: Set<String> = setOf("dark", "light", "system")

/** The valid [UserPrefs.experienceLevel] values (SPEC §6 D6). Anything else → `simple` (default). */
val EXPERIENCE_LEVELS: Set<String> = setOf("simple", "pro")

/**
 * Plain `key=value` line codec, dependency-free (no kotlinx.serialization needed for 4 scalars).
 * Mirrors the project's hand-rolled-store convention (the old `FileFolderStore` line format).
 *
 * Tolerant by design: unknown keys are ignored, missing keys fall back to defaults, a malformed
 * float for `uiScale` falls back to the default, and a parsed `uiScale` is clamped into
 * [UI_SCALE_MIN]..[UI_SCALE_MAX] (defensive — never trust the file).
 */
object PrefCodec {
    fun encode(p: UserPrefs): String = buildString {
        append("uiScale=").append(p.uiScale).append('\n')
        append("accentName=").append(p.accentName).append('\n')
        append("theme=").append(p.theme).append('\n')
        append("density=").append(p.density).append('\n')
        append("locale=").append(p.locale).append('\n')
        // Comma-joined action ids (ids are snake_case — never contain a comma). Stable order for a
        // deterministic round-trip. Empty set ⇒ "confirmedActions=" ⇒ decodes back to emptySet.
        append("confirmedActions=").append(p.confirmedActions.sorted().joinToString(",")).append('\n')
        append("experienceLevel=").append(p.experienceLevel).append('\n')
        append("voiceBeta=").append(p.voiceBeta).append('\n')
    }

    fun decode(text: String): UserPrefs {
        val defaults = UserPrefs()
        val map = HashMap<String, String>()
        for (rawLine in text.lineSequence()) {
            val line = rawLine.trim()
            if (line.isEmpty()) continue
            val eq = line.indexOf('=')
            if (eq <= 0) continue                       // skip blank-key / no-separator lines
            val key = line.substring(0, eq).trim()
            val value = line.substring(eq + 1).trim()
            if (key.isNotEmpty()) map[key] = value
        }
        val uiScale = map["uiScale"]?.toFloatOrNull()?.coerceIn(UI_SCALE_MIN, UI_SCALE_MAX)
            ?: defaults.uiScale
        return UserPrefs(
            uiScale = uiScale,
            accentName = map["accentName"]?.takeIf { it.isNotEmpty() } ?: defaults.accentName,
            theme = map["theme"]?.takeIf { it in THEME_OPTIONS } ?: defaults.theme,
            density = map["density"]?.takeIf { it.isNotEmpty() } ?: defaults.density,
            locale = map["locale"]?.takeIf { it in setOf("en", "zh-Hans", "zh-Hant") } ?: defaults.locale,
            confirmedActions = map["confirmedActions"]
                ?.split(",")
                ?.map { it.trim() }
                ?.filter { it.isNotEmpty() }
                ?.toSet()
                ?: defaults.confirmedActions,
            experienceLevel = map["experienceLevel"]?.takeIf { it in EXPERIENCE_LEVELS }
                ?: defaults.experienceLevel,
            // Only an explicit "true" turns the preview on; a missing/garbage value stays OFF (the
            // byte-identical default) so an old prefs file never accidentally enables affordances.
            voiceBeta = map["voiceBeta"]?.let { it.equals("true", ignoreCase = true) }
                ?: defaults.voiceBeta,
        )
    }
}
