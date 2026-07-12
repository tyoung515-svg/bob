package com.bobclaw.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * A profile's unattended cron schedule (P5 scheduler). Carried on the `/api/profiles`
 * envelope as `schedule: {cron, task, face_hint?}`. A profile fires on its [cron] only
 * when BOTH [cron] and [task] are present (mirrors `core.scheduler.run_tick`, which skips
 * a cron with no task). This is LIVE data — the Home "scheduled fires" tile (U1/D2) binds
 * to it directly; no app-side mock.
 */
@Serializable
data class Schedule(
    val cron: String = "",
    val task: String = "",
    @SerialName("face_hint") val faceHint: String? = null,
) {
    /** A schedule the scheduler will actually fire: a cron AND a task (the run's prompt). */
    val isFireable: Boolean
        get() = cron.isNotBlank() && task.isNotBlank()
}
