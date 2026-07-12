package com.bobclaw.model

import kotlinx.datetime.Instant
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime

/** One upcoming scheduled run: which profile, its cron, and the next fire (null if none soon). */
data class UpcomingFire(
    val profile: String,
    val cron: String,
    val task: String,
    val nextFire: Instant?,
)

/**
 * Derive the upcoming scheduled fires from live profile envelopes (`/api/profiles`).
 * Pure — clock ([now]) injected — so it is unit-testable. Mirrors the scheduler's own skip
 * rule (`core.scheduler.run_tick`): a profile is included ONLY when it carries a fireable
 * schedule (a cron AND a task). Results are sorted by next fire, soonest first; profiles whose
 * cron yields no fire within the search horizon sort last (nextFire == null).
 */
fun upcomingFires(
    profiles: List<Team>,
    now: Instant,
    tz: TimeZone,
): List<UpcomingFire> =
    profiles
        .mapNotNull { p ->
            val sched = p.schedule ?: return@mapNotNull null
            if (!sched.isFireable) return@mapNotNull null
            UpcomingFire(
                profile = p.name,
                cron = sched.cron,
                task = sched.task,
                nextFire = CronNextFire.next(sched.cron, now, tz),
            )
        }
        .sortedWith(compareBy(nullsLast<Instant>()) { it.nextFire })

private val MONTHS = listOf(
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

/**
 * A compact, locale-agnostic label for a fire instant, e.g. "Jul 8, 14:30". Returns null for a
 * null instant (the caller renders an honest "no upcoming fire" instead of a fake time).
 */
fun formatFireTime(instant: Instant?, tz: TimeZone): String? {
    if (instant == null) return null
    val ldt = instant.toLocalDateTime(tz)
    val mon = MONTHS.getOrElse(ldt.monthNumber - 1) { ldt.monthNumber.toString() }
    val hh = ldt.hour.toString().padStart(2, '0')
    val mm = ldt.minute.toString().padStart(2, '0')
    return "$mon ${ldt.dayOfMonth}, $hh:$mm"
}
