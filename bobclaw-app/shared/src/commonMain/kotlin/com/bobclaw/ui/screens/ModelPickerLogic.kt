package com.bobclaw.ui.screens

import com.bobclaw.model.CapabilityBackend

/**
 * MS9-W2 — pure model/backend picker logic for the Chat top-bar chip.
 *
 * The top-bar "Backend" chip already pins a backend per conversation (via `switchModel` +
 * `backendPreference`), but it labeled its rows with raw backend ids (`claude_code`, `minimax`, …)
 * so there was no clean way to deliberately choose a model like "Opus." This turns the same picker
 * into a first-class MODEL picker: each row is labeled with the friendly model name the live
 * `GET /capabilities` registry supplies (`CapabilityBackend.model`, e.g. "Opus 4.8"), annotated by
 * backend id + availability. The pin wire is unchanged — selecting a row still pins that *backend*
 * (backend ↔ model is 1:1 in the registry), and "Auto" clears the pin (→ face routing).
 *
 * Kept pure + separate from Compose so a headless `:shared:jvmTest` guards the label mapping, the
 * Auto row, and honest degradation BEFORE the attended screenshot pass.
 */

/** One selectable row in the Chat model/backend picker.
 *
 *  @property backendId  the backend to pin; `null` marks the "Auto" row (clears the pin).
 *  @property label      primary display text — the friendly model name when the registry has one,
 *                       else the bare backend id (and the localized "Auto" text for the Auto row).
 *  @property secondary  mono caption: `"<backend> · <availability>"` when a friendly model name is
 *                       shown (so the raw backend + its state stay visible), else `null` (Auto row,
 *                       and degraded static rows where availability is unknown).
 *  @property available  drives dimmed rendering; `true` for Auto and for degraded static rows.
 *  @property selected   the row matching the current pin (`selectedBackend`, or Auto when unpinned).
 */
internal data class ModelPickerOption(
    val backendId: String?,
    val label: String,
    val secondary: String?,
    val available: Boolean,
    val selected: Boolean,
)

/**
 * Builds the ordered picker rows: an "Auto" row first (clears the pin), then one row per backend.
 *
 * Prefers the LIVE [liveBackends] from `GET /capabilities` — each carries a human [CapabilityBackend.model]
 * plus [CapabilityBackend.available], so rows get a friendly model name ("Opus 4.8") + a
 * `backend · available/unavailable` caption. When the registry hasn't loaded (empty [liveBackends]),
 * it degrades to [staticBackends] (bare backend ids, availability unknown → no claim made).
 *
 * [selectedBackend] (a backend id, `null` = Auto) marks the active row; if it names a backend absent
 * from both sources (e.g. a pin from an older/renamed registry) a synthetic selected row is appended
 * so the active pin stays visible and honest. The localized [autoLabel]/[availableLabel]/[unavailableLabel]
 * are injected by the caller to keep this function pure + testable.
 */
internal fun buildModelPickerOptions(
    liveBackends: List<CapabilityBackend>,
    staticBackends: List<String>,
    selectedBackend: String?,
    autoLabel: String,
    availableLabel: String,
    unavailableLabel: String,
): List<ModelPickerOption> {
    val options = mutableListOf<ModelPickerOption>()

    // 1. Auto row — always first; selecting it clears the pin (→ face routing).
    options.add(
        ModelPickerOption(
            backendId = null,
            label = autoLabel,
            secondary = null,
            available = true,
            selected = selectedBackend == null,
        ),
    )

    // 2. Backend rows — the live registry when present (friendly model names), else the static ids.
    if (liveBackends.isNotEmpty()) {
        for (b in liveBackends) {
            val hasModel = !b.model.isNullOrBlank()
            val avail = if (b.available) availableLabel else unavailableLabel
            options.add(
                ModelPickerOption(
                    backendId = b.backend,
                    label = if (hasModel) b.model!! else b.backend,
                    // Show the raw backend alongside a friendly name; when the label already IS the
                    // id, drop it and just annotate availability.
                    secondary = if (hasModel) "${b.backend} · $avail" else avail,
                    available = b.available,
                    selected = selectedBackend == b.backend,
                ),
            )
        }
    } else {
        for (id in staticBackends) {
            options.add(
                ModelPickerOption(
                    backendId = id,
                    label = id,
                    secondary = null,
                    available = true,
                    selected = selectedBackend == id,
                ),
            )
        }
    }

    // 3. Keep an off-registry pin visible: if the selected backend matched no row above, append it.
    if (selectedBackend != null && options.none { it.backendId == selectedBackend }) {
        options.add(
            ModelPickerOption(
                backendId = selectedBackend,
                label = selectedBackend,
                secondary = null,
                available = true,
                selected = true,
            ),
        )
    }

    return options
}

/**
 * MS9-W4 (fix D) — the Chat top-bar chip label: the SELECTED row's friendly label, or [autoLabel]
 * only when nothing is pinned (no selected row). Because [buildModelPickerOptions] always emits
 * exactly one selected row whenever a backend is pinned (the live row, the static row, or the
 * step-3 off-registry synthetic row), this returns "Auto" IFF the pin is truly clear — so a freshly
 * picked model's name shows immediately instead of falling back to "Auto". Pure so a headless test
 * guards "a pinned backend never renders as Auto".
 */
internal fun chatBackendChipLabel(options: List<ModelPickerOption>, autoLabel: String): String =
    options.firstOrNull { it.selected }?.label ?: autoLabel
