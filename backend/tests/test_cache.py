"""Tests for the permanent response cache, against a fake OpenF1 with no network."""

from __future__ import annotations

import gzip
import json
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cache import ResponseCache  # noqa: E402
from app.config import Settings  # noqa: E402
from app.models import Session  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from app.providers.openf1 import OpenF1Provider, cache_policy, direct_local_request  # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def session(start: datetime, hours: float = 2.0) -> Session:
    return Session(session_key=1, date_start=start, date_end=start + timedelta(hours=hours))


class TestCachePolicy(unittest.TestCase):
    def test_unknown_session_is_never_permanent(self):
        self.assertEqual(cache_policy(None, NOW), "recent")
        self.assertEqual(cache_policy(Session(session_key=1), NOW), "recent")

    def test_future_session_is_never_permanent(self):
        # The bug this guards: a pre-session "no rows" answer cached forever.
        self.assertEqual(cache_policy(session(NOW + timedelta(days=3)), NOW), "recent")

    def test_inside_the_live_window(self):
        self.assertEqual(cache_policy(session(NOW - timedelta(minutes=20)), NOW), "live")
        self.assertEqual(cache_policy(session(NOW + timedelta(minutes=20)), NOW), "live")

    def test_just_finished_waits_to_settle(self):
        finished_an_hour_ago = session(NOW - timedelta(hours=3))
        self.assertEqual(cache_policy(finished_an_hour_ago, NOW, settle_hours=24), "recent")

    def test_settled_session_is_permanent(self):
        self.assertEqual(cache_policy(session(NOW - timedelta(days=2)), NOW), "permanent")

    def test_settle_period_is_configurable(self):
        finished_an_hour_ago = session(NOW - timedelta(hours=3))
        self.assertEqual(cache_policy(finished_an_hour_ago, NOW, settle_hours=0.5), "permanent")


class TestEmptyMarkers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cache = ResponseCache(Path(self.dir.name))

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_marker_reads_back_as_no_rows(self):
        self.cache.set_disk_empty("/laps", {"session_key": 1}, ttl_seconds=60)
        self.assertEqual(self.cache.get_disk("/laps", {"session_key": 1}), [])

    def test_expired_empty_marker_is_a_miss(self):
        self.cache.set_disk_empty("/laps", {"session_key": 1}, ttl_seconds=-1)
        self.assertIsNone(self.cache.get_disk("/laps", {"session_key": 1}))

    def test_real_rows_never_expire(self):
        self.cache.set_disk("/laps", {"session_key": 1}, [{"lap_number": 1}])
        self.assertEqual(self.cache.get_disk("/laps", {"session_key": 1}), [{"lap_number": 1}])


class FakeOpenF1:
    """Answers like OpenF1 and counts every request that reaches it."""

    def __init__(self, session_start: datetime, laps: list[dict]):
        self.session_start = session_start
        self.laps = laps
        self.requests: list[str] = []
        # When set, every non-catalog request is refused the way OpenF1 does
        # while a live session is running.
        self.live_lock = False
        # When set, every non-catalog request fails with an ordinary server error.
        self.outage = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        self.requests.append(f"{path}?{request.url.query.decode()}")
        if self.outage and path != "sessions":
            return httpx.Response(503, json={"detail": "unavailable"})
        if self.live_lock and path != "sessions":
            return httpx.Response(401, json={"detail": (
                "Live F1 session in progress. Global API access (including past sessions) "
                "is restricted to authenticated users until the session ends."
            )})
        if path == "sessions":
            start = self.session_start
            return httpx.Response(200, json=[{
                "session_key": 1,
                "date_start": start.isoformat(),
                "date_end": (start + timedelta(hours=2)).isoformat(),
            }])
        if path == "laps":
            driver = request.url.params.get("driver_number")
            rows = [r for r in self.laps if driver is None or str(r["driver_number"]) == driver]
            return httpx.Response(200, json=rows) if rows else httpx.Response(404, json={})
        return httpx.Response(404, json={})


class TestProviderCaching(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.settings = Settings(cache_dir=Path(self.dir.name))

    async def asyncTearDown(self):
        self.dir.cleanup()

    def provider(self, fake: FakeOpenF1) -> OpenF1Provider:
        client = httpx.AsyncClient(
            base_url=self.settings.provider.base_url, transport=httpx.MockTransport(fake)
        )
        return OpenF1Provider(self.settings, client=client)

    async def test_settled_session_is_served_from_disk_after_a_restart(self):
        laps = [{"driver_number": 1, "lap_number": 1, "lap_duration": 90.0}]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(days=5), laps)
        first = self.provider(fake)
        await first.laps(1)
        await first.aclose()
        upstream = len(fake.requests)

        # A fresh provider has an empty memory tier, like a restarted server.
        second = self.provider(fake)
        result = await second.laps(1)
        await second.aclose()

        self.assertEqual(len(result), 1)
        self.assertEqual(second.upstream_requests, 0)
        self.assertEqual(len(fake.requests), upstream)

    async def test_one_whole_field_request_serves_every_driver(self):
        laps = [
            {"driver_number": n, "lap_number": 1, "lap_duration": 90.0 + n} for n in (1, 4, 16)
        ]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(days=5), laps)
        provider = self.provider(fake)
        for number in (1, 4, 16):
            mine = await provider.laps(1, number)
            self.assertEqual([lap.driver_number for lap in mine], [number])
        await provider.aclose()

        lap_requests = [r for r in fake.requests if r.startswith("laps")]
        self.assertEqual(len(lap_requests), 1)
        self.assertNotIn("driver_number", lap_requests[0])

    async def test_session_that_has_not_run_is_not_written_to_disk(self):
        fake = FakeOpenF1(datetime.now(timezone.utc) + timedelta(days=3), laps=[])
        provider = self.provider(fake)
        self.assertEqual(await provider.laps(1), [])
        await provider.aclose()
        self.assertEqual(provider.cache_stats()["disk_entries"], 0)

    async def test_empty_answer_for_settled_session_is_retried_later(self):
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(days=5), laps=[])
        provider = self.provider(fake)
        await provider.laps(1)
        await provider.aclose()

        marker = provider._cache.get_disk("/laps", {"session_key": 1})
        self.assertEqual(marker, [])
        path = provider._cache._path("/laps", {"session_key": 1})
        self.assertTrue(path.exists())
        # The marker carries an expiry rather than standing forever.
        with gzip.open(path, "rt") as fh:
            self.assertGreater(json.load(fh)["empty_until"], time.time())

    async def test_last_good_copy_is_served_while_openf1_locks_access(self):
        # Finished an hour ago: not settled, so never cached permanently.
        laps = [{"driver_number": 1, "lap_number": 1, "lap_duration": 90.0}]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(hours=3), laps)
        first = self.provider(fake)
        self.assertEqual(len(await first.laps(1)), 1)
        await first.aclose()

        fake.live_lock = True
        second = self.provider(fake)
        result = await second.laps(1)
        await second.aclose()
        self.assertEqual([lap.lap_number for lap in result], [1])

    async def test_live_lock_without_a_saved_copy_says_why(self):
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(hours=3), laps=[])
        fake.live_lock = True
        provider = self.provider(fake)
        with self.assertRaises(ProviderError) as caught:
            await provider.laps(1)
        await provider.aclose()
        self.assertIn("live F1 session", str(caught.exception))
        self.assertNotIn("Sponsor key", str(caught.exception))

    def provider_with_local(self, fake: FakeOpenF1, local_laps: list[dict]) -> OpenF1Provider:
        settings = Settings(cache_dir=Path(self.dir.name), openf1_local_url="http://local.test/v1")
        public = httpx.AsyncClient(base_url=settings.provider.base_url, transport=httpx.MockTransport(fake))
        provider = OpenF1Provider(settings, client=public)
        local = FakeOpenF1(datetime.now(timezone.utc) - timedelta(minutes=30), local_laps)
        provider._local_client = httpx.AsyncClient(
            base_url="http://local.test/v1", transport=httpx.MockTransport(local)
        )
        return provider

    async def test_local_recording_answers_only_requests_made_on_this_machine(self):
        recorded = [{"driver_number": 1, "lap_number": 7, "lap_duration": 95.0}]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(minutes=30), laps=[])
        fake.live_lock = True
        provider = self.provider_with_local(fake, recorded)

        token = direct_local_request.set(True)
        try:
            mine = await provider.laps(1)
        finally:
            direct_local_request.reset(token)
        self.assertEqual([lap.lap_number for lap in mine], [7])

        # The same request arriving through a tunnel never sees the recording.
        with self.assertRaises(ProviderError):
            await provider.laps(1)
        await provider.aclose()

    async def test_local_recording_is_not_used_for_an_ordinary_outage(self):
        recorded = [{"driver_number": 1, "lap_number": 7, "lap_duration": 95.0}]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(minutes=30), laps=[])
        fake.outage = True
        provider = self.provider_with_local(fake, recorded)
        provider._cfg = replace(provider._cfg, max_retries=1)
        token = direct_local_request.set(True)
        try:
            with self.assertRaises(ProviderError):
                await provider.laps(1)
        finally:
            direct_local_request.reset(token)
        self.assertFalse(provider.live_lock_active())
        await provider.aclose()

    async def test_local_recording_is_never_cached(self):
        recorded = [{"driver_number": 1, "lap_number": 7, "lap_duration": 95.0}]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(minutes=30), laps=[])
        fake.live_lock = True
        provider = self.provider_with_local(fake, recorded)
        token = direct_local_request.set(True)
        try:
            await provider.laps(1)
        finally:
            direct_local_request.reset(token)
        for tier in (provider._cache.get_memory, provider._cache.get_stale, provider._cache.get_disk):
            self.assertIsNone(tier("/laps", {"session_key": 1}))
        await provider.aclose()

    async def test_missing_driver_list_borrows_names_for_cars_that_ran(self):
        laps = [{"driver_number": n, "lap_number": 1, "lap_duration": 90.0} for n in (1, 44)]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(days=5), laps)
        provider = self.provider(fake)
        # An earlier session's list on disk: car 99 did not run in session 1.
        provider._cache.set_disk("/drivers", {"session_key": 0}, [
            {"driver_number": 1, "name_acronym": "NOR", "team_name": "McLaren"},
            {"driver_number": 99, "name_acronym": "OLD"},
        ])
        drivers = await provider.drivers(1)
        await provider.aclose()
        self.assertEqual([(d.driver_number, d.name_acronym) for d in drivers], [(1, "NOR"), (44, None)])

    async def test_settled_data_never_writes_a_last_good_copy(self):
        laps = [{"driver_number": 1, "lap_number": 1, "lap_duration": 90.0}]
        fake = FakeOpenF1(datetime.now(timezone.utc) - timedelta(days=5), laps)
        provider = self.provider(fake)
        await provider.laps(1)
        await provider.aclose()
        self.assertIsNone(provider._cache.get_stale("/laps", {"session_key": 1}))


if __name__ == "__main__":
    unittest.main()
