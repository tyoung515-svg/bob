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
)

/** Default no-op store (no persistence) — used by callers/platforms that don't wire one. */
object NoopPrefStore : PrefStore {
    override fun load(): UserPrefs = UserPrefs()
    override fun save(prefs: UserPrefs) {}
}

/** Inclusive bounds for the functional UI-scale control (DESIGN §5). */
const val UI_SCALE_MIN = 0.8f
const val UI_SCALE_MAX = 1.5f

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
            theme = map["theme"]?.takeIf { it.isNotEmpty() } ?: defaults.theme,
            density = map["density"]?.takeIf { it.isNotEmpty() } ?: defaults.density,
        )
    }
}
