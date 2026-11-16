"""Offline regression tests. All files live under tests/, never the real DB/key."""
import json
import os
import sqlite3
import ssl
import subprocess
import tempfile
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from deepseek_usage import core

LOCAL = timezone(timedelta(hours=8))
START = datetime(2026, 11, 2, 9, 0, tzinfo=LOCAL)


def balance(total=10.0, currency="CNY"):
    return {"ok": True, "available": True, "status": "available", "total": total,
            "currency": currency, "topped_up": total, "granted": 0.0}


def payload(total: object = "10", topped="10", granted="0", available=True, currency="CNY"):
    return {"is_available": available, "balance_infos": [{"currency": currency,
            "total_balance": total, "topped_up_balance": topped, "granted_balance": granted}]}


class Clock:
    def __init__(self):
        self.wall = START
        self.tick = 100.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.tick += seconds


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".unit-", dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        def restore_timezone():
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()
        self.addCleanup(restore_timezone)
        # Fail loudly if a test accidentally tries live credentials, DB or network.
        for name in ("hermes_home", "api_key", "subprocess.run", "urllib.request.urlopen"):
            p = patch("deepseek_usage.core." + name,
                      side_effect=AssertionError("test attempted live I/O: " + name))
            p.start()
            self.addCleanup(p.stop)

    def service(self, **kwargs):
        options = dict(db_path=self.root / "state.db", state_path=self.root / "spend.json",
                       now=lambda: self.clock.wall, monotonic=lambda: self.clock.tick,
                       key_reader=lambda: "fake-offline-key", balance_loader=lambda _: balance())
        options.update(kwargs)
        return core.UsageService(**options)

    def make_db(self, rows=None):
        with closing(sqlite3.connect(self.root / "state.db")) as conn:
            conn.execute("""CREATE TABLE session_model_usage (
                session_id TEXT, model TEXT, billing_provider TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cache_read_tokens INTEGER, api_call_count INTEGER,
                estimated_cost_usd REAL, last_seen REAL)""")
            if rows:
                conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()


class SpendTests(IsolatedTest):
    def sample(self, service, total):
        b = balance(total)
        b["observed_at"] = int(self.clock.wall.timestamp())
        return service.track_spend(b)

    def test_cross_day_rebases(self):
        service = self.service()
        self.sample(service, 10)
        self.clock.advance(10)
        self.assertEqual(self.sample(service, 8)["spent_today"], 2)
        self.clock.advance(86400)
        result = self.sample(service, 7)
        self.assertTrue(result["rebased"])
        self.assertEqual(result["spent_today"], 0)
        self.assertEqual(result["opening"], 7)
        self.assertEqual(result["tracking_since"], int(self.clock.wall.timestamp()))

    def test_recharge_rebases_against_last_not_opening(self):
        service = self.service()
        first = self.sample(service, 10)
        self.clock.advance(10)
        self.sample(service, 8)
        self.clock.advance(10)
        # Small top-up still below original opening: old opening + 0.5 missed it.
        result = self.sample(service, 8.2)
        self.assertTrue(result["rebased"])
        self.assertEqual(result["opening"], 8.2)
        self.assertEqual(result["spent_today"], 2)
        self.assertEqual(result["rebases"], 1)
        self.assertEqual(result["tracking_since"], first["tracking_since"])
        self.assertEqual(result["opening_at"], int(self.clock.wall.timestamp()))
        self.clock.advance(10)
        self.assertEqual(self.sample(service, 8.1)["spent_today"], 2.1)

    def test_decreases_accumulate_without_rebasing(self):
        service = self.service()
        first = self.sample(service, 10)
        for value, spent in ((9.9, 0.1), (9.7, 0.3), (9.7, 0.3), (0, 10)):
            self.clock.advance(10)
            result = self.sample(service, value)
            self.assertFalse(result["rebased"])
            self.assertEqual(result["spent_today"], spent)
            self.assertEqual(result["opening"], 10)
            self.assertEqual(result["opening_at"], first["opening_at"])

    def test_missed_segment_is_healed_from_balance_delta(self):
        """A stale state.json that won a write race must not undercount spend."""
        service = self.service()
        self.sample(service, 9.56)
        path = service._state_path()
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(last=4.19, spent=1.89)   # older accumulator, newer balance
        path.write_text(json.dumps(state), encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.sample(service, 4.19)["spent_today"], 5.37)

    def test_external_write_is_picked_up_instead_of_clobbered(self):
        service = self.service()
        self.sample(service, 10)
        self.clock.advance(10)
        self.sample(service, 9)
        path = service._state_path()
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(last=8.0, spent=2.0)     # another writer moved the file
        path.write_text(json.dumps(state), encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.sample(service, 7.5)["spent_today"], 2.5)

    def test_unchanged_sample_does_not_rewrite_or_reread_state(self):
        service = self.service()
        with patch.object(core.tempfile, "NamedTemporaryFile", wraps=tempfile.NamedTemporaryFile) as writer:
            self.sample(service, 10)
            self.clock.advance(60)
            with patch.object(Path, "read_text", side_effect=AssertionError("unexpected reread")):
                self.sample(service, 10)
                self.sample(service, 10)
            self.assertEqual(writer.call_count, 1)

    def test_original_state_migrates_and_survives_restart(self):
        state = dict(date="2026-11-02", currency="CNY", opening=10, last=9,
                     opening_at=int(START.timestamp()), last_at=int(START.timestamp()))
        path = self.root / "spend.json"
        path.write_text(json.dumps(state))
        self.assertEqual(self.sample(self.service(), 8)["spent_today"], 2)
        self.assertEqual(self.sample(self.service(), 7)["spent_today"], 3)

    def test_currency_change_rebases_instead_of_mixing_units(self):
        service = self.service()
        self.sample(service, 10)
        result = service.track_spend(balance(5, "USD"))
        self.assertTrue(result["rebased"])
        self.assertEqual(result["spent_today"], 0)

    def test_failed_balance_never_creates_state(self):
        service = self.service()
        self.assertEqual(service.track_spend(core._failure("timeout")), {})
        self.assertFalse((self.root / "spend.json").exists())

    def test_persistence_failure_is_separate_from_balance_and_usage(self):
        self.make_db()
        service = self.service(state_path=self.root)  # replacing a directory must fail
        result = service.summary()
        self.assertTrue(result["balance"]["ok"])
        self.assertFalse(result["balance"]["spend_ok"])
        self.assertTrue(result["today"]["ok"])
        self.assertIn("保存", result["balance"]["spend_error"])


def make_prorated_db(test, usage_rows, messages=()):
    """Full schema: rows carry first_seen, and messages record activity."""
    with closing(sqlite3.connect(test.root / "state.db")) as conn:
        conn.execute("""CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, input_tokens INTEGER,
            output_tokens INTEGER, cache_read_tokens INTEGER, api_call_count INTEGER,
            estimated_cost_usd REAL, first_seen REAL, last_seen REAL)""")
        conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                         usage_rows)
        conn.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL, content TEXT)")
        conn.executemany("INSERT INTO messages VALUES (?,?,?)", messages)
        conn.commit()


class UsageTests(IsolatedTest):
    def test_aggregation_filters_local_calendar_day_and_model_provider(self):
        start = datetime(2026, 11, 2, tzinfo=LOCAL).timestamp()
        self.make_db([
            ("previous", "deepseek-x", "deepseek", 999, 999, 0, 1, 9, start - 1),
            # UTC Nov 1, but local Nov 2: must be INCLUDED.
            ("s1", "deepseek-a", "custom", 100, 10, 70, 2, 0.1, start),
            ("s1", "alias", "deepseek", 20, 2, 5, 1, 0.02, start + 86400 - 1),
            ("s2", "deepseek-b", "deepseek", 50, 5, 10, 3, 0.05, start + 10),
            ("other", "gpt-x", "openai", 999, 999, 0, 1, 9, start + 100),
            ("future", "deepseek-x", "deepseek", 999, 999, 0, 1, 9, start + 86400),
        ])
        service = self.service()
        result = service.usage_for("2026-11-02")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sessions"], 2)
        self.assertEqual((result["input"], result["output"], result["tokens"]), (170, 17, 187))
        self.assertEqual((result["cache_read"], result["calls"]), (85, 6))
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.17)
        self.assertEqual(len(result["models"]), 3)
        self.assertEqual(result["scope"], "prorated_today")
        self.assertEqual(result["last_seen"], start + 86399)
        self.assertEqual(service.usage_for("2026-11-01")["tokens"], 1998)

    def test_empty_usage_is_successful_zero(self):
        self.make_db()
        result = self.service().usage_for()
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 0)
        self.assertEqual(result["models"], [])
        self.assertIsNone(result["last_seen"])

    def test_session_carried_over_from_yesterday_counts_only_todays_share(self):
        now = self.clock.wall
        midnight = datetime.combine(now.date(), dtime(0)).timestamp()
        make_prorated_db(
            self,
            [("old", "deepseek-flash", "deepseek", 1_000_000, 0, 0, 10, 0.0,
              midnight - 86400, now.timestamp())],
            [("old", midnight - 3600, "y" * 900), ("old", midnight + 60, "x" * 100)])
        value = self.service().usage_for()
        self.assertEqual(value["sessions"], 1)
        self.assertEqual(value["input"], 100_000)   # 10% of the session's text is today
        self.assertEqual(value["calls"], 1)

    def test_session_started_today_is_counted_in_full(self):
        now = self.clock.wall
        midnight = datetime.combine(now.date(), dtime(0)).timestamp()
        make_prorated_db(
            self,
            [("new", "deepseek-flash", "deepseek", 1_000_000, 0, 0, 10, 0.0,
              midnight + 60, now.timestamp())],
            [("new", midnight - 86400, "y" * 900), ("new", midnight + 120, "x" * 100)])
        self.assertEqual(self.service().usage_for()["input"], 1_000_000)

    def test_carried_session_without_message_history_does_not_inflate_today(self):
        now = self.clock.wall
        midnight = datetime.combine(now.date(), dtime(0)).timestamp()
        make_prorated_db(
            self,
            [("ghost", "deepseek-flash", "deepseek", 1_000_000, 0, 0, 10, 0.0,
              midnight - 86400, now.timestamp())])
        value = self.service().usage_for()
        self.assertTrue(value["ok"])
        self.assertEqual((value["input"], value["sessions"]), (0, 0))

    def test_missing_db_is_not_created_and_error_is_cached(self):
        service = self.service()
        with patch.object(core.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            for _ in range(3):
                self.assertEqual(service.usage_for()["error"], "no_state_db")
            self.assertEqual(connect.call_count, 1)
        self.assertFalse((self.root / "state.db").exists())

    def test_connection_is_readonly_and_closed_even_after_query_error(self):
        # Intentionally incompatible schema.
        with closing(sqlite3.connect(self.root / "state.db")) as conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
            conn.commit()
        connect = sqlite3.connect
        captured = []
        def spy(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith("?mode=ro"))
            conn = connect(database_uri, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE forbidden (id INTEGER)")
            captured.append(conn)
            return conn
        with patch.object(core.sqlite3, "connect", side_effect=spy):
            self.assertEqual(self.service().usage_for()["error"], "sqlite")
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")

    def test_balance_failure_does_not_hide_usage(self):
        self.make_db([("s", "deepseek-a", "deepseek", 100, 10, 0, 1, 0.1, START.timestamp())])
        for error in ("timeout", "tls", "auth", "network", "invalid_response"):
            with self.subTest(error=error):
                service = self.service(balance_loader=lambda _: core._failure(error))
                result = service.summary()
                self.assertTrue(result["ok"])
                self.assertFalse(result["balance"]["ok"])
                self.assertEqual(result["balance"]["error"], error)
                self.assertTrue(result["today"]["ok"])
                self.assertEqual(result["today"]["tokens"], 110)
        self.assertFalse((self.root / "spend.json").exists())

    def test_usage_failure_does_not_hide_balance(self):
        result = self.service().summary()
        self.assertTrue(result["ok"])
        self.assertTrue(result["balance"]["ok"])
        self.assertFalse(result["today"]["ok"])


class CacheTests(IsolatedTest):
    def test_ttls_force_refresh_and_no_per_request_io(self):
        self.make_db()
        reader, loader = Mock(return_value="fake"), Mock(return_value=balance())
        service = self.service(key_reader=reader, balance_loader=loader)
        with patch.object(core.sqlite3, "connect", wraps=sqlite3.connect) as connect, \
             patch.object(core.tempfile, "NamedTemporaryFile", wraps=tempfile.NamedTemporaryFile) as writer:
            for _ in range(100):
                service.summary()
            self.assertEqual((reader.call_count, loader.call_count, connect.call_count, writer.call_count),
                             (1, 1, 1, 1))
            self.clock.advance(16)
            service.summary()
            self.assertEqual((loader.call_count, connect.call_count), (1, 2))
            self.clock.advance(5)
            service.summary()
            self.assertEqual((loader.call_count, connect.call_count), (2, 2))
            service.summary(force=True)
            self.assertEqual((loader.call_count, connect.call_count), (3, 3))
            self.assertEqual(writer.call_count, 1)

    def test_failed_requests_and_missing_key_are_cached(self):
        for key in (None, "fake"):
            with self.subTest(key=key):
                reader = Mock(return_value=key)
                loader = Mock(return_value=core._failure("timeout"))
                service = self.service(key_reader=reader, balance_loader=loader)
                for _ in range(5):
                    service.fetch_balance()
                self.assertEqual(reader.call_count, 1)
                self.assertEqual(loader.call_count, int(key is not None))
                self.clock.advance(21)
                service.fetch_balance()
                self.assertEqual(reader.call_count, 2)

    def test_midnight_invalidates_balance_and_usage_even_within_ttl(self):
        self.make_db()
        self.clock.wall = datetime(2026, 11, 2, 23, 59, 59, tzinfo=LOCAL)
        loader = Mock(return_value=balance())
        service = self.service(balance_loader=loader)
        first = service.summary()
        self.clock.advance(2)
        with patch.object(core.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            second = service.summary()
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(loader.call_count, 2)
        self.assertNotEqual(first["today"]["date"], second["today"]["date"])
        self.assertTrue(second["balance"]["rebased"])

    def test_concurrent_summary_is_single_flight(self):
        self.make_db()
        loader = Mock(return_value=balance())
        service = self.service(balance_loader=loader)
        with patch.object(core.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: service.summary(), range(16)))
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(r["today"]["ok"] for r in results))


class TransportTests(IsolatedTest):
    def test_curl_failure_classification_and_no_secret_echo(self):
        for exit_code, error in ((28, "timeout"), (60, "tls"), (35, "tls"), (6, "network")):
            with self.subTest(exit_code=exit_code), \
                 patch.object(core.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], exit_code, "", "fake-secret-should-not-be-echoed")):
                result = core._request_balance("fake-secret-should-not-be-echoed")
                self.assertEqual(result["error"], error)
                self.assertNotIn("fake-secret", json.dumps(result))

    def test_key_is_stdin_only_and_payload_is_validated(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(payload()) + "\n200", "")
        with patch.object(core.subprocess, "run", return_value=result) as run:
            self.assertTrue(core._request_balance("fake-key")["ok"])
            args, kwargs = run.call_args
            self.assertNotIn("fake-key", repr(args))
            self.assertIn("Authorization: Bearer fake-key", kwargs["input"])
            self.assertEqual(args[0][1], "--disable")
            self.assertNotIn("--insecure", args[0])

    def test_subprocess_timeout_is_sanitized(self):
        with patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("fake-secret", 9)):
            result = core._request_balance("fake-secret")
        self.assertEqual(result["error"], "timeout")
        self.assertNotIn("fake-secret", json.dumps(result))

    def test_http_errors_and_bad_json_are_not_zero_balance(self):
        for response, error in (("{}\n401", "auth"), ("{}\n403", "auth"),
                                ("{}\n429", "rate_limited"), ("{}\n500", "http"),
                                ("not-json\n200", "invalid_response"), ("{}\n200", "invalid_response")):
            with self.subTest(response=response), patch.object(core.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, response, "")):
                result = core._request_balance("fake")
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], error)

    def test_zero_grant_only_and_unavailable_are_distinct_successes(self):
        for p, status in ((payload("0", "0", "0", False), "zero"),
                          (payload("5", "0", "5"), "grant_only"),
                          (payload("5", "5", "0", False), "unavailable"),
                          (payload(), "available")):
            with self.subTest(status=status):
                result = core._balance_payload(p)
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], status)

    def test_missing_or_nonfinite_amounts_are_not_zero(self):
        for amount in (None, "garbage", "NaN", "Infinity", "-1"):
            with self.subTest(amount=amount):
                self.assertEqual(core._balance_payload(payload(amount))["error"], "invalid_response")
        self.assertEqual(core._balance_payload([])["error"], "invalid_response")

    def test_currency_selection_does_not_invent_rmb(self):
        usd = payload(currency="USD")
        self.assertEqual(core._balance_payload(usd)["currency"], "USD")
        usd["balance_infos"].append(payload(currency="CNY")["balance_infos"][0])
        self.assertEqual(core._balance_payload(usd)["currency"], "CNY")

    def test_curl_missing_uses_urllib_with_classified_errors(self):
        for cause, error in ((ssl.SSLError("fake-secret"), "tls"),
                             (TimeoutError("fake-secret"), "timeout"),
                             (OSError("fake-secret"), "network")):
            with self.subTest(error=error), \
                 patch.object(core.subprocess, "run", side_effect=FileNotFoundError()), \
                 patch.object(core.urllib.request, "urlopen", side_effect=urllib.error.URLError(cause)):
                result = core._request_balance("fake-secret")
                self.assertEqual(result["error"], error)
                self.assertNotIn("fake-secret", json.dumps(result))

    def test_invalid_key_does_not_start_transport(self):
        self.assertEqual(core._request_balance("fake\nheader")["error"], "invalid_key")


class PriceTests(unittest.TestCase):
    """RMB estimates come from DeepSeek's own price list — never from an FX guess."""

    def test_peak_window_is_beijing_weekdays_only(self):
        self.assertTrue(core.is_peak(datetime(2026, 11, 2, 10, 0, tzinfo=core.BEIJING)))   # 周一上午
        self.assertFalse(core.is_peak(datetime(2026, 11, 2, 13, 0, tzinfo=core.BEIJING)))  # 午休
        self.assertFalse(core.is_peak(datetime(2026, 11, 7, 10, 0, tzinfo=core.BEIJING)))  # 周六
        self.assertTrue(core.is_peak(datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)))    # 10:00 北京

    def test_estimate_charges_hit_miss_and_output_separately(self):
        at = datetime(2026, 11, 2, 22, 0, tzinfo=core.BEIJING)
        est = core.estimate_cny(
            [("deepseek-flash", 1_000_000, 1_000_000, 1_000_000, at.timestamp(), at.timestamp())], at)
        self.assertEqual((est["low"], est["high"]), (5.02, 10.04))
        self.assertEqual(est["cny"], 5.02)          # 整段都在空闲时段
        self.assertIsNone(est["peak_share"])        # 零跨度行不参与占比统计

    def test_window_straddling_peak_boundary_is_split(self):
        start = datetime(2026, 11, 2, 11, 0, tzinfo=core.BEIJING).timestamp()   # 11:00 高峰内
        end = datetime(2026, 11, 2, 13, 0, tzinfo=core.BEIJING).timestamp()     # 13:00 午休
        est = core.estimate_cny([("deepseek-flash", 1_000_000, 0, 0, start, end)],
                                datetime(2026, 11, 2, 20, 0, tzinfo=core.BEIJING))
        self.assertAlmostEqual(est["peak_share"], 0.5, places=3)   # 一半高峰、一半空闲
        self.assertAlmostEqual(est["cny"], 1.5, places=2)          # 1 元与 2 元各半

    def test_weekend_window_is_never_peak(self):
        start = datetime(2026, 11, 7, 10, 0, tzinfo=core.BEIJING).timestamp()   # 周六上午
        end = datetime(2026, 11, 7, 11, 0, tzinfo=core.BEIJING).timestamp()
        est = core.estimate_cny([("deepseek-flash", 1_000_000, 0, 0, start, end)],
                                datetime(2026, 11, 7, 11, 0, tzinfo=core.BEIJING))
        self.assertEqual(est["peak_share"], 0.0)
        self.assertEqual(est["cny"], 1.0)

    def test_unknown_model_is_reported_not_priced_as_zero(self):
        at = datetime(2026, 11, 2, 22, 0, tzinfo=core.BEIJING)
        est = core.estimate_cny([("mystery-model", 1_000_000, 0, 0,
                                  at.timestamp(), at.timestamp())], at)
        self.assertEqual(est["unpriced"], ["mystery-model"])
        self.assertEqual(est["low"], 0.0)


if __name__ == "__main__":
    unittest.main()
