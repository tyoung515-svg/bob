package com.bobclaw.ui.screens

import com.bobclaw.model.Action
import com.bobclaw.model.Capabilities
import com.bobclaw.model.Team
import com.bobclaw.model.TeamDraft
import com.bobclaw.model.TeamSlot
import com.bobclaw.ui.components.ActionDisposition
import com.bobclaw.ui.components.dispositionFor

/**
 * MS9-W6 — pure logic for **Ask-Bob-on-Teams composition** (the finding: the Teams tab had no way
 * to change a team / no manager spot). Compose-free + module-`internal` so it is fully unit-testable
 * (mirrors [com.bobclaw.ui.components.dispositionFor] in `AskBobLogic`); the Teams screen renders
 * these decisions, it does not re-derive them. Three responsibilities:
 *   1. **Team ↔ Draft mapping** — turn an existing [Team] into an editable [TeamDraft] so the
 *      conversational refine flow can EDIT it (not only build a new one).
 *   2. **The manager surface** — extract the team's *manager* (the `apex` role — the orchestrator/
 *      lead; core `teams.py`: "``apex`` (manager)") so the UI can label it "Manager" + show who holds it.
 *   3. **The apply guardrail** — a team create/replace is a `reversible` write (registry `create_team`),
 *      so it routes through the SAME D11/D12 disposition path the Ask-Bob bubble uses — never a silent write.
 */

/** The role that IS the team's manager — the orchestrator/lead. Core `teams.py`: ``apex`` (manager). */
internal const val MANAGER_ROLE = "apex"

/**
 * The U3 registry action that gates a team create/replace write (`reversible`, D12 confirm-once —
 * see `core/actions/registry.py`). The Teams-page Save shares this id (and its persisted confirm-once)
 * with the Ask-Bob bubble's `create_team` chip, so confirming in one place skips the prompt in both.
 */
internal const val APPLY_TEAM_ACTION_ID = "create_team"

/**
 * Convert an existing [Team] (built-in or custom) into an editable [TeamDraft] for the refine flow.
 * Drops the `builtin` flag + `schedule` (not builder-owned); carries name / roles / shape /
 * protocol_bounds so an edit round-trip (load → refine → save) is lossless for the fields the builder owns.
 */
internal fun Team.toDraft(): TeamDraft = TeamDraft(
    name = name,
    roles = roles,
    shape = shape,
    protocolBounds = protocolBounds,
)

/** The **manager spot** of a roster — the FIRST `apex` slot (may carry a blank backend when unassigned),
 *  or null when the team declares no manager at all. */
internal fun managerSlot(roles: Map<String, List<TeamSlot>>): TeamSlot? =
    roles[MANAGER_ROLE]?.firstOrNull()

/** **Who holds the manager spot** — the apex primary backend, or null when unmanaged / unassigned. */
internal fun managerBackend(roles: Map<String, List<TeamSlot>>): String? =
    managerSlot(roles)?.backend?.takeIf { it.isNotBlank() }

internal fun Team.managerBackend(): String? = managerBackend(roles)
internal fun TeamDraft.managerBackend(): String? = managerBackend(roles)

/**
 * The persisted roster: drop blank-backend slots + roles that end up empty. Pure, so Save can PREVIEW
 * exactly what core will store (core's `_as_slots` drops empty-backend slots the same way).
 */
internal fun cleanedRoles(draft: TeamDraft): Map<String, List<TeamSlot>> =
    draft.roles
        .mapValues { (_, slots) -> slots.filter { it.backend.isNotBlank() } }
        .filterValues { it.isNotEmpty() }

/** The apply (create/replace) action from the live registry, or null (older gateway / degraded doc). */
internal fun applyTeamAction(caps: Capabilities?): Action? =
    caps?.actions?.firstOrNull { it.id == APPLY_TEAM_ACTION_ID }

/**
 * The disposition for **applying** (create/replace) a composed team, reusing the D11/D12 guardrail path
 * ([dispositionFor]) — a team write is NEVER a silent auto-write. When the registry doesn't surface
 * `create_team` (older gateway / degraded document) we FAIL SAFE to confirm-once against the action id:
 * a team write always confirms at least once, and never auto-writes without the registry.
 */
internal fun applyTeamDisposition(
    caps: Capabilities?,
    confirmedActionIds: Set<String>,
    mutatingExecutedThisTurn: Int = 0,
): ActionDisposition {
    val action = applyTeamAction(caps)
        ?: return if (APPLY_TEAM_ACTION_ID in confirmedActionIds) ActionDisposition.EXECUTE
        else ActionDisposition.CONFIRM_FIRST
    return dispositionFor(action, confirmedActionIds, mutatingExecutedThisTurn)
}
