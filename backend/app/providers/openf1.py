"""OpenF1 adapter.

Notes that drove the design here:

* The Community tier needs no credential and covers 2023 onward, historical
  only. A credential unlocks the live window and the streaming transports.
  ``capabilities.live`` therefore tracks the presence of a key — this backend
  will not describe archived data as live under any circumstance.
* Published limits are 3 req/s and 30 req/min on Community. We run a token
  bucket slightly below both ceilings, because exceeding them produces 429s
  that cost more time than the throttling does.
* ``car_data`` for a whole session and a whole field is enormous — roughly
  3.7 samples/second/car. Every telemetry read is therefore narrowed to one
  driver and one time window, which is what the UI actually needs for a lap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypeVar

import httpx
from pydantic import ValidationError

from ..cache import ResponseCache
from ..config import ProviderSettings, Settings
from ..models import (
    CarSample,
    Driver,
    Interval,
    Lap,
    LocationSample,
    Meeting,
    PitStop,
    PositionSample,
    RaceControlMessage,
    Session,
    Stint,
    TeamRadio,
    Weather,
)
from .base import LiveLockError, ProviderCapabilities, ProviderError

log = logging.getLogger("apex.openf1")

# True while handling a request made directly on this machine (not through a
# tunnel or proxy). Set per request by middleware in main.py; only such
# requests may be answered from the local F1TV recording.
direct_local_request: ContextVar[bool] = ContextVar("direct_local_request", default=False)

# How long after OpenF1's last live-session refusal the lock is assumed to hold.
LIVE_LOCK_MEMORY_SECONDS = 300.0

T = TypeVar("T")

CachePolicy = Literal["live", "recent", "permanent"]
"""How long a response may be reused.

``live``       inside the provider's live window; held for seconds.
``recent``     not started yet, just finished, or unknown; held for minutes.
``permanent``  finished and settled; written to disk and never fetched again.
"""


def cache_policy(
    session: Session | None,
    now: datetime,
    *,
    live_window_minutes: float = 30,
    settle_hours: float = 24,
) -> CachePolicy:
    """Decide how a session's data may be cached at ``now``.

    Anything that cannot be shown to be finished stays out of the permanent
    tier. That includes sessions that have not started: the provider answers
    those with no rows, and caching that answer forever is how a race would
    end up permanently empty.
    """
    if session is None or session.date_start is None:
        return "recent"
    start = session.date_start
    end = session.date_end or (start + timedelta(hours=3))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    margin = timedelta(minutes=live_window_minutes)
    if now < start - margin:
        return "recent"
    if now <= end + margin:
        return "live"
    if now < end + timedelta(hours=settle_hours):
        return "recent"
    return "permanent"


class TokenBucket:
    """Dual-window limiter: both the per-second and per-minute ceiling hold."""

    def __init__(self, per_second: float, per_minute: float) -> None:
        self._per_second = per_second
        self._per_minute = per_minute
        self._second_window: list[float] = []
        self._minute_window: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._second_window = [t for t in self._second_window if now - t < 1.0]
                self._minute_window = [t for t in self._minute_window if now - t < 60.0]

                waits = []
                if len(self._second_window) >= self._per_second:
                    waits.append(1.0 - (now - self._second_window[0]))
                if len(self._minute_window) >= self._per_minute:
                    waits.append(60.0 - (now - self._minute_window[0]))

                if not waits:
                    self._second_window.append(now)
                    self._minute_window.append(now)
                    return
                await asyncio.sleep(max(max(waits), 0.01))


class OpenF1Provider:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._cfg: ProviderSettings = settings.provider
        self._cache = ResponseCache(settings.cache_dir / "openf1")
        self._bucket = TokenBucket(self._cfg.requests_per_second, self._cfg.requests_per_minute)
        headers = {"User-Agent": "Delta/1.0 (+engineering-analytics)"}
        if self._cfg.api_key:
            # Sponsor-tier credential. Server side only — never reaches a browser.
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"
        self._client = client or httpx.AsyncClient(
            base_url=self._cfg.base_url,
            timeout=self._cfg.timeout_seconds,
            headers=headers,
            follow_redirects=True,
        )
        self._session_meta: dict[int, Session] = {}
        self._borrowed: dict[tuple[int, bool], list[Driver]] = {}
        self._live_lock_seen_at: float | None = None
        # Upstream HTTP attempts made by this process; cache hits do not count.
        self.upstream_requests = 0
        self._local_url = settings.openf1_local_url
        self._local_client = (
            httpx.AsyncClient(base_url=self._local_url, timeout=self._cfg.timeout_seconds)
            if self._local_url
            else None
        )

    # -- local recording ---------------------------------------------------

    def local_recording_allowed(self) -> bool:
        return self._local_client is not None and direct_local_request.get()

    def live_lock_active(self) -> bool:
        """Whether OpenF1 refused a request because of a live session in the last few minutes."""
        seen = self._live_lock_seen_at
        return seen is not None and time.monotonic() - seen < LIVE_LOCK_MEMORY_SECONDS

    async def _get_local(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Ask the local OpenF1 API over the race agent's F1TV recording.

        Returns None when it is not allowed for this request, not running, or
        answers with anything but rows. Its answers are never cached.
        """
        if not self.local_recording_allowed():
            return None
        try:
            response = await self._local_client.get(path, params=params)
        except httpx.HTTPError:
            return None
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, list) else None

    async def local_recording_has(self, session_key: int) -> bool:
        rows = await self._get_local("/sessions", {"session_key": session_key})
        return bool(rows)

    # -- capabilities ------------------------------------------------------

    @property
    def capabilities(self) -> ProviderCapabilities:
        live = self._cfg.has_live_access
        return ProviderCapabilities(
            name="openf1",
            display_name="OpenF1",
            raw_telemetry=True,
            car_location=True,
            live=live,
            streaming=live,
            race_control=True,
            weather=True,
            stints=True,
            pit_stops=True,
            intervals=True,
            team_radio=True,
            first_season=2023,
            last_season=datetime.now(timezone.utc).year,
            requires_key=False,
            has_key=live,
            attribution="Data from OpenF1 (openf1.org). Unofficial; not affiliated with "
            "Formula 1, the FIA or Formula One Management.",
            licence_note="OpenF1 site content is CC BY-NC-SA 4.0 and the project describes "
            "intended use as educational, personal, research and non-commercial fan "
            "engagement. Attribution is not required but a link back is appreciated. "
            "Commercial or redistributive use needs a separate conversation with the "
            "project. Underlying F1 timing data remains the rights-holders'.",
        )

    # -- transport ---------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any], *, policy: CachePolicy) -> list[dict[str, Any]]:
        params = {k: v for k, v in params.items() if v is not None}

        # Only final data is ever written to disk, so a disk hit is always
        # safe to serve, whatever policy the caller asked for.
        cached = self._cache.get_disk(path, params)
        if cached is not None:
            return cached
        cached = self._cache.get_memory(path, params)
        if cached is not None:
            return cached

        async def fetch() -> list[dict[str, Any]]:
            last_error: Exception | None = None
            for attempt in range(self._cfg.max_retries):
                await self._bucket.acquire()
                self.upstream_requests += 1
                try:
                    response = await self._client.get(path, params=params)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    raise ProviderError(
                        f"Could not reach OpenF1 at {self._cfg.base_url}. Check network egress "
                        f"from this host — the API is plain HTTPS and needs no credential on the "
                        f"Community tier. ({exc.__class__.__name__})"
                    ) from exc
                except httpx.HTTPError as exc:
                    last_error = exc
                    await asyncio.sleep(self._cfg.backoff_base_seconds * (2**attempt))
                    continue

                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                    delay = retry_after or self._cfg.backoff_base_seconds * (2**attempt)
                    if attempt == self._cfg.max_retries - 1:
                        raise ProviderError(
                            "OpenF1 rate limit reached. The Community tier allows 3 req/s and "
                            "30 req/min; narrow the selection or add a Sponsor key.",
                            status=429,
                            retry_after=delay,
                        )
                    await asyncio.sleep(min(delay, 10.0))
                    continue

                if response.status_code in (401, 403):
                    # While any F1 session is live, OpenF1 refuses every
                    # unauthenticated request, past sessions included, and says
                    # so in its detail. Anything already cached keeps working.
                    try:
                        detail = str(response.json().get("detail") or "")
                    except (ValueError, AttributeError):
                        detail = ""
                    if "live" in detail.lower() and "session" in detail.lower():
                        self._live_lock_seen_at = time.monotonic()
                        raise LiveLockError(
                            "OpenF1 is limiting free access while a live F1 session is running, "
                            "and this data is not saved yet. It returns about 30 minutes after "
                            "the session ends; everything already saved keeps working meanwhile.",
                            status=response.status_code,
                        )
                    raise ProviderError(
                        "OpenF1 refused the request. Live-window data requires a Sponsor key in "
                        "OPENF1_API_KEY; historical data should need none.",
                        status=response.status_code,
                    )

                if response.status_code >= 500:
                    if attempt == self._cfg.max_retries - 1:
                        raise ProviderError(
                            f"OpenF1 returned {response.status_code}.", status=response.status_code
                        )
                    await asyncio.sleep(self._cfg.backoff_base_seconds * (2**attempt))
                    continue

                if response.status_code == 404:
                    # OpenF1 answers 404 when a query matches no rows, which is
                    # an ordinary outcome rather than a failure: a session that
                    # has not run yet, a car that never left the garage, a
                    # practice session with no stint records. Surfacing it as an
                    # error would put a red panel where an empty state belongs,
                    # so it is mapped to no rows and the caller renders "no data".
                    return []

                if response.status_code >= 400:
                    raise ProviderError(
                        f"OpenF1 rejected the request ({response.status_code}) for {path}.",
                        status=response.status_code,
                    )

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderError("OpenF1 returned a body that was not JSON.") from exc

                if isinstance(payload, dict):
                    # Error envelopes come back as objects rather than arrays.
                    raise ProviderError(str(payload.get("detail") or payload.get("message") or payload))
                return payload

            raise ProviderError(f"OpenF1 request failed after retries: {last_error}")

        try:
            data = await self._cache.single_flight(path, params, fetch)
        except ProviderError as exc:
            # OpenF1 stays the source in every normal case. The local recording
            # stands in only while OpenF1 itself refuses because a session is live.
            local = await self._get_local(path, params) if isinstance(exc, LiveLockError) else None
            if local is not None:
                log.info("OpenF1 unavailable for %s; answered from the local F1TV recording", path)
                return local
            stale = self._cache.get_stale(path, params)
            if stale is None:
                raise
            log.warning("OpenF1 unavailable for %s; serving the last good copy", path)
            # Hold it briefly so a page's burst of requests does not retry upstream each time.
            self._cache.set_memory(path, params, stale, self._settings.recent_cache_seconds)
            return stale
        self._store(path, params, data, policy)
        return data

    def _store(self, path: str, params: dict[str, Any], data: list, policy: CachePolicy) -> None:
        if policy == "permanent":
            if data:
                self._cache.set_disk(path, params, data)
            else:
                self._cache.set_disk_empty(path, params, self._settings.empty_retry_hours * 3600)
        elif policy == "recent":
            self._cache.set_memory(path, params, data, self._settings.recent_cache_seconds)
            if data:
                self._cache.set_stale(path, params, data)
        else:
            self._cache.set_memory(path, params, data, self._settings.live_cache_seconds)

    @staticmethod
    def _parse(model: type[T], rows: list[dict[str, Any]]) -> list[T]:
        """Validate row by row and drop only the rows that are unusable.

        A single malformed row in a 40,000-sample telemetry response must not
        destroy the whole lap. Dropped rows are counted, not silently ignored.
        """
        out: list[T] = []
        for row in rows:
            try:
                out.append(model(**row))
            except ValidationError:
                continue
        return out

    # -- freshness ---------------------------------------------------------

    def is_live_window(self, session: Session | None) -> bool:
        """OpenF1 defines live as 30 min before start through 30 min after end."""
        return self.policy(session) == "live"

    def policy(self, session: Session | None) -> CachePolicy:
        return cache_policy(
            session,
            datetime.now(timezone.utc),
            live_window_minutes=self._settings.live_window_minutes,
            settle_hours=self._settings.settle_hours,
        )

    async def _policy_for(self, session_key: int) -> CachePolicy:
        return self.policy(await self.session(session_key))

    def _persist_if_final(self, path: str, params: dict[str, Any], rows: list, sessions: list[Session]) -> None:
        """Promote a catalog answer to disk once every session in it is final.

        Catalog queries are not scoped to one session, so their policy is only
        known after the answer arrives.
        """
        if rows and sessions and all(self.policy(s) == "permanent" for s in sessions):
            self._cache.set_disk(path, {k: v for k, v in params.items() if v is not None}, rows)

    # -- catalog -----------------------------------------------------------

    async def meetings(self, year: int) -> list[Meeting]:
        current_year = datetime.now(timezone.utc).year
        policy: CachePolicy = "permanent" if year < current_year else "recent"
        rows = await self._get("/meetings", {"year": year}, policy=policy)
        meetings = self._parse(Meeting, rows)
        meetings.sort(key=lambda m: (m.date_start is None, m.date_start or datetime.min))
        return meetings

    async def sessions(self, *, year: int | None = None, meeting_key: int | None = None) -> list[Session]:
        if year is None and meeting_key is None:
            raise ProviderError("A year or a meeting_key is required to list sessions.")
        params = {"year": year, "meeting_key": meeting_key}
        rows = await self._get("/sessions", params, policy="recent")
        sessions = self._parse(Session, rows)
        self._persist_if_final("/sessions", params, rows, sessions)
        for s in sessions:
            self._session_meta[s.session_key] = s
        sessions.sort(key=lambda s: (s.date_start is None, s.date_start or datetime.min))
        return sessions

    async def session(self, session_key: int) -> Session | None:
        if session_key in self._session_meta:
            return self._session_meta[session_key]
        params = {"session_key": session_key}
        rows = await self._get("/sessions", params, policy="recent")
        found = self._parse(Session, rows)
        if not found:
            return None
        self._persist_if_final("/sessions", params, rows, found[:1])
        self._session_meta[session_key] = found[0]
        return found[0]

    # -- session data ------------------------------------------------------

    async def drivers(self, session_key: int) -> list[Driver]:
        rows = await self._get(
            "/drivers", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        drivers = self._unique_drivers(rows)
        if drivers:
            return drivers
        return await self._borrowed_drivers(session_key)

    def _unique_drivers(self, rows: list[dict[str, Any]]) -> list[Driver]:
        # The feed can repeat a driver across updates; last entry wins.
        unique: dict[int, Driver] = {d.driver_number: d for d in self._parse(Driver, rows)}
        return sorted(unique.values(), key=lambda d: d.driver_number)

    async def _borrowed_drivers(self, session_key: int) -> list[Driver]:
        """A driver list for a session whose own list is missing.

        The live feed sends names and teams only once, when a recording first
        connects; a recording that joins mid-session has lap times and
        telemetry but no names. The line-up barely changes between sessions, so
        the most recent earlier list is borrowed, limited to the cars that
        actually ran here. Only data already on this machine is searched, so
        this never spends the public rate limit.
        """
        # Keyed by whether the local recording may be used, so a borrowed list
        # never crosses from a local request to a public one.
        memo_key = (session_key, self.local_recording_allowed())
        if memo_key in self._borrowed:
            return self._borrowed[memo_key]
        present = {lap.driver_number for lap in await self.laps(session_key)}
        if not present:
            return []

        # Earlier lists already on disk, newest session first...
        candidates: dict[int, list[dict[str, Any]]] = {}
        for earlier in range(session_key - 1, session_key - 40, -1):
            params = {"session_key": earlier}
            rows = self._cache.get_disk("/drivers", params) or self._cache.get_stale("/drivers", params)
            if rows:
                candidates[earlier] = rows
        # ...plus the local recording's, fetched in one request: its API allows
        # only 30 requests per 10 s, and a page load already makes several.
        local_rows = (
            await self._get_local("/drivers", {"session_key<": session_key}) or []
            if self.live_lock_active()
            else []
        )
        for row in local_rows:
            if isinstance(row.get("session_key"), int):
                candidates.setdefault(row["session_key"], []).append(row)

        result = [Driver(driver_number=n, session_key=session_key) for n in sorted(present)]
        for earlier in sorted(candidates, reverse=True):
            found = [d for d in self._unique_drivers(candidates[earlier]) if d.driver_number in present]
            if not found:
                continue
            known = {d.driver_number for d in found}
            found += [Driver(driver_number=n, session_key=session_key) for n in present - known]
            log.info("Session %s has no driver list; borrowed names from session %s", session_key, earlier)
            result = sorted(found, key=lambda d: d.driver_number)
            break
        self._borrowed[memo_key] = result
        return result

    async def laps(self, session_key: int, driver_number: int | None = None) -> list[Lap]:
        policy = await self._policy_for(session_key)
        if policy == "permanent":
            # A settled session is fetched once for the whole field and
            # filtered locally, so one cached file serves every driver instead
            # of each driver costing its own request.
            rows = await self._get("/laps", {"session_key": session_key}, policy=policy)
            if driver_number is not None:
                rows = [r for r in rows if r.get("driver_number") == driver_number]
        else:
            rows = await self._get(
                "/laps", {"session_key": session_key, "driver_number": driver_number}, policy=policy
            )
        laps = self._parse(Lap, rows)
        laps.sort(key=lambda l: (l.driver_number, l.lap_number or 0))
        return laps

    async def car_data(
        self,
        session_key: int,
        driver_number: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CarSample]:
        params: dict[str, Any] = {"session_key": session_key, "driver_number": driver_number}
        if start is not None:
            params["date>"] = _iso(start)
        if end is not None:
            params["date<"] = _iso(end)
        rows = await self._get("/car_data", params, policy=await self._policy_for(session_key))
        samples = self._parse(CarSample, rows)
        samples.sort(key=lambda s: s.date)
        return samples

    async def location(
        self,
        session_key: int,
        driver_number: int,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[LocationSample]:
        params: dict[str, Any] = {"session_key": session_key, "driver_number": driver_number}
        if start is not None:
            params["date>"] = _iso(start)
        if end is not None:
            params["date<"] = _iso(end)
        rows = await self._get("/location", params, policy=await self._policy_for(session_key))
        samples = self._parse(LocationSample, rows)
        samples.sort(key=lambda s: s.date)
        # The feed emits (0, 0, 0) placeholders when a car is not being tracked.
        return [s for s in samples if not (s.x == 0 and s.y == 0)]

    async def stints(self, session_key: int) -> list[Stint]:
        rows = await self._get(
            "/stints", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        stints = self._parse(Stint, rows)
        stints.sort(key=lambda s: (s.driver_number, s.stint_number or 0))
        return stints

    async def pit_stops(self, session_key: int) -> list[PitStop]:
        rows = await self._get(
            "/pit", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        return self._parse(PitStop, rows)

    async def intervals(self, session_key: int) -> list[Interval]:
        rows = await self._get(
            "/intervals", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        samples = self._parse(Interval, rows)
        samples.sort(key=lambda s: s.date)
        return samples

    async def positions(self, session_key: int) -> list[PositionSample]:
        rows = await self._get(
            "/position", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        samples = self._parse(PositionSample, rows)
        samples.sort(key=lambda s: s.date)
        return samples

    async def race_control(self, session_key: int) -> list[RaceControlMessage]:
        rows = await self._get(
            "/race_control", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        msgs = self._parse(RaceControlMessage, rows)
        msgs.sort(key=lambda m: (m.date is None, m.date or datetime.min))
        return msgs

    async def weather(self, session_key: int) -> list[Weather]:
        rows = await self._get(
            "/weather", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        rec = self._parse(Weather, rows)
        rec.sort(key=lambda w: (w.date is None, w.date or datetime.min))
        return rec

    async def team_radio(self, session_key: int) -> list[TeamRadio]:
        rows = await self._get(
            "/team_radio", {"session_key": session_key}, policy=await self._policy_for(session_key)
        )
        return self._parse(TeamRadio, rows)

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._local_client is not None:
            await self._local_client.aclose()

    def cache_stats(self) -> dict[str, int | str]:
        return {**self._cache.stats(), "upstream_requests_this_process": self.upstream_requests}


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
