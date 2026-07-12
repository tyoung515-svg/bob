package com.bobclaw.ui.components

import com.bobclaw.model.Action
import com.bobclaw.model.Capabilities

/**
 * Pure decision logic for the U5 "Ask Bob" helper bubble (SPEC §3 · D11 tiers · D12 guardrails).
 *
 * Kept free of Compose so it is fully unit-testable (mirrors `SlashPaletteLogic`): the bubble
 * composable renders these decisions, it does not re-derive them. Two responsibilities:
 *   1. **Tool scope** — filter the U3 registry to the actions offered on the current page (`page_scope`).
 *   2. **Risk gating (D11) + guardrails (D12)** — decide, for a chosen action, whether the bubble may
 *      execute it now, must confirm-once first, must route it to Approvals, or is rate-capped.
 */

/** The known risk tiers (must mirror core `core/actions/registry.py` `RISK_TIERS`). */
internal const val RISK_READ = "read"
internal const val RISK_REVERSIBLE = "reversible"
internal const val RISK_GATED = "gated"

/**
 * D12 rate cap: the most MUTATING (non-read) actions the bubble will execute within a single
 * user turn. Beyond this, further mutating actions are refused (RATE_CAPPED) — a runaway-tool guard.
 */
internal const val MUTATING_RATE_CAP = 3

/** What the bubble should do with a chosen action, given the D11 tier + D12 guardrail state. */
internal enum class ActionDisposition {
    /** Execute now: a `read` action, or a `reversible` one already confirmed-once. */
    EXECUTE,
    /** D12 confirm-once: a `reversible` action on its FIRST use for this id — prompt, then execute. */
    CONFIRM_FIRST,
    /** D11 `gated`: never executed by the bubble — surfaces to the Approvals queue for a human. */
    ROUTE_TO_APPROVALS,
    /** D12 rate cap: too many mutating actions already ran this turn — refuse. */
    RATE_CAPPED,
}

/** A mutating action is anything that is not a pure `read` (it changes server state → guarded). */
internal fun isMutating(action: Action): Boolean = action.risk != RISK_READ

/**
 * The registry actions offered on [page] (case-insensitive `page_scope` membership). Empty when
 * [caps] is null (registry not loaded / fetch failed) — the bubble then offers Guide mode only.
 */
internal fun actionsForPage(caps: Capabilities?, page: String): List<Action> {
    if (caps == null) return emptyList()
    val p = page.trim()
    if (p.isEmpty()) return emptyList()
    return caps.actions.filter { action ->
        action.pageScope.any { it.equals(p, ignoreCase = true) }
    }
}

/**
 * The disposition for executing [action] from the bubble, given the ids the user has already
 * [confirmedActionIds] and how many mutating actions have run this turn ([mutatingExecutedThisTurn]).
 *
 * Order (fail-safe): an unknown/`gated` tier NEVER auto-executes (→ Approvals); a `reversible` on
 * first use confirms once; then the per-turn mutating rate cap applies; otherwise execute.
 */
internal fun dispositionFor(
    action: Action,
    confirmedActionIds: Set<String>,
    mutatingExecutedThisTurn: Int,
): ActionDisposition {
    // D11 + fail-safe: gated (and any tier we don't recognize) is never fired by the bubble.
    if (action.risk != RISK_READ && action.risk != RISK_REVERSIBLE) {
        return ActionDisposition.ROUTE_TO_APPROVALS
    }
    // D12 confirm-once: first use of a reversible-write id → confirm before running.
    if (action.risk == RISK_REVERSIBLE && action.id !in confirmedActionIds) {
        return ActionDisposition.CONFIRM_FIRST
    }
    // D12 rate cap: a mutating action beyond the per-turn budget is refused.
    if (isMutating(action) && mutatingExecutedThisTurn >= MUTATING_RATE_CAP) {
        return ActionDisposition.RATE_CAPPED
    }
    return ActionDisposition.EXECUTE
}

/**
 * D12 consequence toast: a plain-language sentence naming the action, shown while it executes.
 * Appends the undo hint when the API permits an undo (a `reversible` action always carries one).
 */
internal fun consequenceToast(action: Action): String {
    val base = "Bob is doing: ${action.title}. ${action.descriptionPlain}".trim()
    val undo = action.undoHint?.trim()
    return if (!undo.isNullOrEmpty()) "$base (Undo: $undo)" else base
}
