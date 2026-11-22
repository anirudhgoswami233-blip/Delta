"""Live tab contracts, against race-agent output written to a temporary folder."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.routers import live


class LiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "race-agent"
        self.session = self.root / "11396"
        self.session.mkdir(parents=True)
        self.patch = patch.object(live, "get_settings", return_value=Settings(race_agent_dirs=(self.root,)))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def write_state(self, age_seconds=1.0, finished=False, session_dir=None):
        updated = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        (self.root / "live.json").write_text(json.dumps({
            "mode": "replay", "updated_at": updated.isoformat(), "session_key": 11396,
            "session_dir": str(session_dir or self.session), "drivers": [{"num": "12", "tla": "ANT"}],
            "agent": {"finished": finished},
        }))

    async def test_no_agent_is_reported_not_raised(self):
        state = await live.live_state()
        self.assertFalse(state["available"])
        self.assertIn("openf1-race", state["reason"])
        self.assertFalse((await live.live_commentary(after=0))["available"])

    async def test_state_freshness(self):
        self.write_state(age_seconds=2)
        state = await live.live_state()
        self.assertTrue(state["available"])
        self.assertFalse(state["stale"])
        self.assertEqual(state["mode"], "replay")
        self.write_state(age_seconds=60)
        self.assertTrue((await live.live_state())["stale"])
        self.write_state(age_seconds=60, finished=True)  # a finished session is final, not stale
        self.assertFalse((await live.live_state())["stale"])

    async def test_commentary_after_and_glossary(self):
        self.write_state()
        with open(self.session / "commentary.jsonl", "w") as f:
            for i in (1, 2, 3):
                f.write(json.dumps({"i": i, "headline": f"update {i}"}) + "\n")
        (self.session / "glossary.json").write_text(json.dumps({"VSC": "Virtual Safety Car", "gap": "to leader"}))
        items = (await live.live_commentary(after=1))["items"]
        self.assertEqual([item["i"] for item in items], [2, 3])
        terms = (await live.live_glossary())["terms"]
        self.assertEqual([t["term"] for t in terms], ["gap", "VSC"])

    async def test_session_dir_outside_agent_folder_is_ignored(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        (outside / "debrief.md").write_text("secret")
        self.write_state(session_dir=outside)
        self.assertFalse((await live.live_debrief())["available"])

    async def test_telemetry_is_columnar_per_driver(self):
        self.write_state()
        (self.root / "telemetry.json").write_text(json.dumps({
            "updated_at": "x", "fields": ["date", "speed", "n_gear"],
            "drivers": {"12": [["t1", 300, 7], ["t2", 310, 8]]},
        }))
        data = await live.live_telemetry(driver="12")
        self.assertEqual(data["samples"]["speed"], [300, 310])
        self.assertEqual(data["samples"]["n_gear"], [7, 8])
        self.assertEqual((await live.live_telemetry(driver="99"))["samples"]["speed"], [])


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, openf1_dir="/nonexistent-openf1"):
        return Settings(race_agent_control_url="http://127.0.0.1:65530", openf1_dir=Path(openf1_dir))

    async def test_missing_openf1_project_is_explained(self):
        with patch.object(live, "get_settings", return_value=self.settings()):
            state = await live.control_status()
        self.assertFalse(state["available"])
        self.assertIn("openf1 project was not found", state["reason"])

    async def test_remote_controller_is_never_spawned(self):
        remote = Settings(race_agent_control_url="http://192.0.2.1:8010", openf1_dir=Path("/nonexistent"))
        with patch.object(live, "get_settings", return_value=remote), \
                patch.object(live, "_ping", return_value=False), \
                patch.object(live.subprocess, "Popen") as popen:
            reason = await live._ensure_controller()
        popen.assert_not_called()
        self.assertIn("not reachable", reason)

    async def test_requests_are_forwarded_and_errors_keep_their_detail(self):
        import httpx

        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.content))
            if request.url.path == "/live/start":
                return httpx.Response(409, json={"detail": "F1TV token missing or expired. Log in first."})
            return httpx.Response(200, json={"job": None, "pending": None})

        original = httpx.AsyncClient
        with patch.object(live, "get_settings", return_value=self.settings()), \
                patch.object(live.httpx, "AsyncClient",
                             side_effect=lambda **kw: original(**kw, transport=httpx.MockTransport(handler))):
            status = await live.control_replay({"session_key": 11396, "speed": 4})
            self.assertTrue(status["available"])
            with self.assertRaises(live.HTTPException) as ctx:
                await live.control_live({})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("token", ctx.exception.detail)
        self.assertEqual(seen[0][:2], ("POST", "/replay/start"))
        self.assertIn(b"11396", seen[0][2])


if __name__ == "__main__":
    unittest.main()
