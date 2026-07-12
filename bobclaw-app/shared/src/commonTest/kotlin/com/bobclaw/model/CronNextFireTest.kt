package com.bobclaw.model

import kotlinx.datetime.DayOfWeek
import kotlinx.datetime.Instant
import kotlinx.datetime.TimeZone
import kotlinx.datetime.toLocalDateTime
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

/**
 * Headless guard for the pure cron next-fire evaluator that powers the Home "scheduled fires"
 * tile (U1). All cases evaluate in UTC with an injected clock, so they are deterministic.
 */
class CronNextFireTest {

    private val utc = TimeZone.UTC
    private fun at(iso: String) = Instant.parse(iso)
    private fun next(cron: String, after: String) = CronNextFire.next(cron, at(after), utc)

    @Test
    fun every_minute_is_the_next_minute_seconds_ignored() {
        assertEquals(at("2026-07-08T10:01:00Z"), next("* * * * *", "2026-07-08T10:00:30Z"))
    }

    @Test
    fun step_minutes_land_on_the_next_quarter() {
        assertEquals(at("2026-07-08T10:15:00Z"), next("*/15 * * * *", "2026-07-08T10:07:00Z"))
    }

    @Test
    fun top_of_every_hour() {
        assertEquals(at("2026-07-08T11:00:00Z"), next("0 * * * *", "2026-07-08T10:30:00Z"))
    }

    @Test
    fun daily_time_rolls_to_tomorrow_when_past() {
        assertEquals(at("2026-07-09T09:00:00Z"), next("0 9 * * *", "2026-07-08T10:00:00Z"))
    }

    @Test
    fun daily_time_is_today_when_still_ahead() {
        assertEquals(at("2026-07-08T09:00:00Z"), next("0 9 * * *", "2026-07-08T08:30:00Z"))
    }

    @Test
    fun specific_month_and_day_rolls_to_next_year() {
        assertEquals(at("2027-01-01T00:00:00Z"), next("0 0 1 1 *", "2026-07-08T00:00:00Z"))
    }

    @Test
    fun day_of_week_lands_on_the_right_weekday() {
        // "0 0 * * 1" = every Monday at 00:00. Assert the property (weekday + time), not a hardcoded date.
        val fire = next("0 0 * * 1", "2026-07-08T12:00:00Z")!!
        val ldt = fire.toLocalDateTime(utc)
        assertEquals(DayOfWeek.MONDAY, ldt.dayOfWeek)
        assertEquals(0, ldt.hour)
        assertEquals(0, ldt.minute)
    }

    @Test
    fun sunday_accepts_both_0_and_7() {
        val a = next("0 0 * * 0", "2026-07-08T12:00:00Z")!!
        val b = next("0 0 * * 7", "2026-07-08T12:00:00Z")!!
        assertEquals(a, b)
        assertEquals(DayOfWeek.SUNDAY, a.toLocalDateTime(utc).dayOfWeek)
    }

    @Test
    fun list_of_minutes_matches_earliest() {
        assertEquals(at("2026-07-08T10:20:00Z"), next("10,20,40 * * * *", "2026-07-08T10:15:00Z"))
    }

    @Test
    fun invalid_expressions_return_null() {
        assertNull(next("not a cron", "2026-07-08T10:00:00Z"))
        assertNull(next("* * * *", "2026-07-08T10:00:00Z"))       // 4 fields
        assertNull(next("60 * * * *", "2026-07-08T10:00:00Z"))    // minute out of range
        assertNull(next("* 24 * * *", "2026-07-08T10:00:00Z"))    // hour out of range
        assertNull(next("", "2026-07-08T10:00:00Z"))
    }
}
