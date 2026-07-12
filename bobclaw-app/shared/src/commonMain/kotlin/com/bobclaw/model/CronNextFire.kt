package com.bobclaw.model

import kotlinx.datetime.DateTimeUnit
import kotlinx.datetime.Instant
import kotlinx.datetime.LocalDate
import kotlinx.datetime.LocalDateTime
import kotlinx.datetime.TimeZone
import kotlinx.datetime.atStartOfDayIn
import kotlinx.datetime.isoDayNumber
import kotlinx.datetime.plus
import kotlinx.datetime.toInstant
import kotlinx.datetime.toLocalDateTime

/**
 * A tiny, self-contained standard 5-field cron evaluator: `minute hour day-of-month month
 * day-of-week`. Pure (no I/O, clock injected), so it is unit-testable under `:shared:jvmTest`.
 * Used by the Home "scheduled fires" tile (U1) to render each scheduled profile's *next fire*
 * from its live `schedule.cron` — the display companion to `core.scheduler.fire_bucket_for`
 * (which computes the *previous* bucket for exactly-once firing).
 *
 * Supported per field: `*`, `a`, `a-b`, lists `a,b,c`, and steps `*​/n` / `a-b/n` / `a/n`.
 * Day-of-week uses cron numbering (0 or 7 = Sunday, 1 = Monday … 6 = Saturday). Standard cron
 * OR-semantics: when BOTH day-of-month and day-of-week are restricted (neither is `*`), a day
 * matches if EITHER matches; if only one is restricted, that one must match.
 */
object CronNextFire {

    /**
     * The first fire STRICTLY AFTER [after], evaluated in [tz]. Returns null for an invalid
     * cron expression or when no fire occurs within a one-year search horizon (e.g. an
     * impossible date). Minute-resolution (seconds ignored), matching the core scheduler.
     */
    fun next(cron: String, after: Instant, tz: TimeZone): Instant? {
        val fields = cron.trim().split(Regex("\\s+"))
        if (fields.size != 5) return null

        val minutes = parseField(fields[0], 0, 59) ?: return null
        val hours = parseField(fields[1], 0, 23) ?: return null
        val doms = parseField(fields[2], 1, 31) ?: return null
        val months = parseField(fields[3], 1, 12) ?: return null
        // Day-of-week: normalize 7 -> 0 (both mean Sunday) into cron space 0..6.
        val dowRaw = parseField(fields[4], 0, 7) ?: return null
        val dows = dowRaw.map { if (it == 7) 0 else it }.toSet()

        val domRestricted = fields[2].trim() != "*"
        val dowRestricted = fields[4].trim() != "*"

        // Start candidate: the minute strictly after `after`.
        val startLdt = after.toLocalDateTime(tz)
        val truncated = LocalDateTime(
            startLdt.year, startLdt.monthNumber, startLdt.dayOfMonth,
            startLdt.hour, startLdt.minute, 0, 0,
        )
        var cur = truncated.toInstant(tz).plus(1, DateTimeUnit.MINUTE, tz)

        // Search horizon: one year of minutes is the absolute ceiling; day/month skipping
        // keeps the real iteration count tiny for ordinary crons (hourly/daily resolve fast).
        var guard = 0
        val maxGuard = 366 * 24 * 60 + 60
        while (guard++ < maxGuard) {
            val ldt = cur.toLocalDateTime(tz)
            if (ldt.monthNumber !in months) {
                cur = firstOfNextMonth(ldt, tz)
                continue
            }
            if (!dayMatches(ldt, doms, dows, domRestricted, dowRestricted)) {
                cur = startOfNextDay(ldt, tz)
                continue
            }
            if (ldt.hour !in hours || ldt.minute !in minutes) {
                cur = cur.plus(1, DateTimeUnit.MINUTE, tz)
                continue
            }
            return cur
        }
        return null
    }

    private fun dayMatches(
        ldt: LocalDateTime,
        doms: Set<Int>,
        dows: Set<Int>,
        domRestricted: Boolean,
        dowRestricted: Boolean,
    ): Boolean {
        val domOk = ldt.dayOfMonth in doms
        val cronDow = ldt.dayOfWeek.isoDayNumber % 7 // ISO Mon=1..Sun=7 -> cron Sun=0..Sat=6
        val dowOk = cronDow in dows
        return when {
            domRestricted && dowRestricted -> domOk || dowOk
            domRestricted -> domOk
            dowRestricted -> dowOk
            else -> true
        }
    }

    private fun startOfNextDay(ldt: LocalDateTime, tz: TimeZone): Instant =
        LocalDate(ldt.year, ldt.monthNumber, ldt.dayOfMonth)
            .plus(1, DateTimeUnit.DAY)
            .atStartOfDayIn(tz)

    private fun firstOfNextMonth(ldt: LocalDateTime, tz: TimeZone): Instant =
        LocalDate(ldt.year, ldt.monthNumber, 1)
            .plus(1, DateTimeUnit.MONTH)
            .atStartOfDayIn(tz)

    /**
     * Expand one cron field into the set of allowed integers within [lo]..[hi], or null if the
     * field is syntactically invalid (unknown token / out-of-range) so the caller fails soft.
     */
    private fun parseField(field: String, lo: Int, hi: Int): Set<Int>? {
        val out = mutableSetOf<Int>()
        for (partRaw in field.trim().split(",")) {
            val part = partRaw.trim()
            if (part.isEmpty()) return null
            val slash = part.split("/")
            if (slash.size > 2) return null
            val step = if (slash.size == 2) slash[1].toIntOrNull()?.takeIf { it > 0 } ?: return null else 1
            val rangeSpec = slash[0]

            val (start, end) = when {
                rangeSpec == "*" -> lo to hi
                rangeSpec.contains("-") -> {
                    val rr = rangeSpec.split("-")
                    if (rr.size != 2) return null
                    val a = rr[0].toIntOrNull() ?: return null
                    val b = rr[1].toIntOrNull() ?: return null
                    a to b
                }
                else -> {
                    val v = rangeSpec.toIntOrNull() ?: return null
                    // A bare "a/n" means a..hi step n; a bare "a" means just {a}.
                    if (slash.size == 2) v to hi else v to v
                }
            }
            if (start < lo || end > hi || start > end) return null
            var v = start
            while (v <= end) {
                out.add(v)
                v += step
            }
        }
        return if (out.isEmpty()) null else out
    }
}
