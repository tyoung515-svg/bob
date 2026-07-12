package com.bobclaw.model

import kotlinx.datetime.Instant
import kotlinx.datetime.TimeZone
import kotlinx.serialization.json.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Guards the Home "scheduled fires" tile data path (U1/D2): (1) the `/api/profiles` envelope's
 * `schedule` block deserializes onto [Team]; (2) [upcomingFires] filters + sorts exactly like the
 * core scheduler's skip rule (cron AND task required). No Compose — pure model logic.
 */
class ScheduledFiresTest {

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    private val utc = TimeZone.UTC
    private val now = Instant.parse("2026-07-08T10:00:00Z")

    @Test
    fun profile_envelope_deserializes_schedule_ignoring_seats() {
        // Shape mirrors a real /api/profiles item: `seats` (unknown to Team) is ignored, `schedule` binds.
        val payload = """
            {
              "name": "sched-fusion-e2e",
              "seats": [{"posture": "framer", "backend": "deepseek_v4_flash"}],
              "shape": "fusion",
              "schedule": {"cron": "0 9 * * *", "task": "ops digest", "face_hint": "assistant"}
            }
        """.trimIndent()
        val team = json.decodeFromString<Team>(payload)
        assertEquals("sched-fusion-e2e", team.name)
        assertEquals("0 9 * * *", team.schedule?.cron)
        assertEquals("ops digest", team.schedule?.task)
        assertEquals("assistant", team.schedule?.faceHint)
        assertTrue(team.schedule!!.isFireable)
    }

    @Test
    fun unscheduled_profile_has_null_schedule() {
        val team = json.decodeFromString<Team>("""{"name": "premium-build", "builtin": true}""")
        assertNull(team.schedule)
    }

    @Test
    fun cron_without_task_is_not_fireable() {
        val s = Schedule(cron = "0 9 * * *", task = "")
        assertFalse(s.isFireable)
    }

    @Test
    fun upcoming_fires_keeps_only_fireable_and_sorts_soonest_first() {
        val profiles = listOf(
            Team(name = "nightly", schedule = Schedule(cron = "0 3 * * *", task = "nightly digest")),
            Team(name = "hourly", schedule = Schedule(cron = "0 * * * *", task = "hourly poll")),
            Team(name = "cron-no-task", schedule = Schedule(cron = "0 9 * * *", task = "")), // filtered
            Team(name = "no-schedule"),                                                       // filtered
        )
        val fires = upcomingFires(profiles, now, utc)

        assertEquals(listOf("hourly", "nightly"), fires.map { it.profile })
        // hourly fires at 11:00 today; nightly at 03:00 tomorrow — hourly is sooner.
        assertEquals(Instant.parse("2026-07-08T11:00:00Z"), fires[0].nextFire)
        assertEquals(Instant.parse("2026-07-09T03:00:00Z"), fires[1].nextFire)
    }

    @Test
    fun profile_with_unfireable_invalid_cron_sorts_last_with_null_fire() {
        val profiles = listOf(
            Team(name = "broken", schedule = Schedule(cron = "not-a-cron", task = "x")),
            Team(name = "good", schedule = Schedule(cron = "0 * * * *", task = "y")),
        )
        val fires = upcomingFires(profiles, now, utc)
        assertEquals(2, fires.size)
        assertEquals("good", fires.first().profile)      // real fire sorts before the null one
        assertEquals("broken", fires.last().profile)
        assertNull(fires.last().nextFire)
    }

    @Test
    fun format_fire_time_is_null_safe_and_compact() {
        assertNull(formatFireTime(null, utc))
        assertEquals("Jul 8, 09:00", formatFireTime(Instant.parse("2026-07-08T09:00:00Z"), utc))
    }
}
