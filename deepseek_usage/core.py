"""Independent account-balance and persisted-usage snapshots (stdlib only).

session_model_usage is cumulative per session/model/route, NOT a daily ledger.
Filtering last_seen by local date selects today's active rows, including their
prior-day tokens. Never add the focusedUsage live stream to these totals.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

BALANCE_URL = "https://api.deepseek.com/user/balance"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
# Both successes AND failures are cached. Manual refresh bypasses both TTLs.
BALANCE_TTL_SECONDS = 20
USAGE_TTL_SECONDS = 15

ERROR_MESSAGES = {
    "no_api_key": "未找到 DeepSeek API key（环境变量或 Codex 配置）",
    "key_unreadable": "无法读取 DeepSeek 密钥配置",
    "invalid_key": "DeepSeek 密钥格式无效",
    "timeout": "余额接口请求超时，可稍后刷新",
    "tls": "余额接口 TLS 证书验证或握手失败；未绕过证书验证",
    "auth": "余额接口认证失败，请检查 DeepSeek API key",
    "rate_limited": "余额接口限流，请稍后刷新",
    "network": "无法连接 DeepSeek 余额接口",
    "http": "DeepSeek 余额接口返回 HTTP 错误",
    "invalid_response": "余额接口响应不含有效金额或币种；未将缺失金额当作零",
    "no_state_db": "未找到 Hermes state.db（只读模式，不会创建数据库）",
    "sqlite": "无法只读查询 Hermes 用量数据库，可能被占用或表结构不兼容",
    "state_io": "无法保存余额观测记录；消耗暂不可可靠计算",
}


def _failure(code: str, **extra) -> dict:
    # Never echo exceptions, stderr, response bodies or credentials to UI/logs.
    return {"ok": False, "error": code, "reason": ERROR_MESSAGES[code], **extra}


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path.home() / ".hermes"


def _number(value) -> float | None:
    try:
        result = float(value)
        return round(result, 4) if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def api_key() -> str | None:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    match = re.search(r'^\s*experimental_bearer_token\s*=\s*"([^"\r\n]+)"', text, re.M)
    return match.group(1).strip() if match else None


def _http_error(status: int) -> dict:
    code = {401: "auth", 403: "auth", 429: "rate_limited"}.get(status, "http")
    return _failure(code, http_status=status)


def _balance_payload(payload) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("is_available"), bool):
        return _failure("invalid_response")
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos or not all(isinstance(i, dict) for i in infos):
        return _failure("invalid_response")
    # Prefer RMB; never relabel another currency as CNY.
    info = next((i for i in infos if i.get("currency") == "CNY"), infos[0])
    currency = info.get("currency")
    amounts = [_number(info.get(k)) for k in
               ("total_balance", "topped_up_balance", "granted_balance")]
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency) \
            or any(v is None for v in amounts):
        return _failure("invalid_response")
    total, topped_up, granted = amounts
    status = ("zero" if total == 0 else "unavailable" if not payload["is_available"]
              else "grant_only" if topped_up == 0 and granted > 0 else "available")
    return {"ok": True, "available": payload["is_available"], "status": status,
            "currency": currency, "total": total, "topped_up": topped_up, "granted": granted}


def _request_balance(key: str) -> dict:
    """curl uses macOS trust; credentials go through stdin, never argv.

    urllib is used only if curl is absent, not after TLS/timeout errors (which
    would double latency). TLS verification stays enabled in both transports.
    """
    if any(c in key for c in ('\r', '\n', '\x00')):
        return _failure("invalid_key")
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    try:
        proc = subprocess.run(
            ["curl", "--disable", "--silent", "--show-error", "--connect-timeout", "3",
             "--max-time", "8", "--config", "-", "--write-out", "\n%{http_code}", BALANCE_URL],
            input=f'header = "Authorization: Bearer {escaped}"\n',
            capture_output=True, text=True, timeout=9,
        )
        if proc.returncode:
            code = ("timeout" if proc.returncode == 28 else "tls" if proc.returncode in
                    (35, 51, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91) else "network")
            return _failure(code)
        raw, _, status = proc.stdout.rpartition("\n")
        if not status.isdigit():
            return _failure("invalid_response")
        if not 200 <= int(status) < 300:
            return _http_error(int(status))
    except FileNotFoundError:
        try:
            request = urllib.request.Request(BALANCE_URL, headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return _http_error(exc.code)
        except (TimeoutError, socket.timeout):
            return _failure("timeout")
        except urllib.error.URLError as exc:
            code = ("tls" if isinstance(exc.reason, ssl.SSLError) else
                    "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network")
            return _failure(code)
        except ssl.SSLError:
            return _failure("tls")
        except (OSError, UnicodeError):
            return _failure("network")
    except subprocess.TimeoutExpired:
        return _failure("timeout")
    except (OSError, UnicodeError):
        return _failure("network")
    try:
        return _balance_payload(json.loads(raw))
    except (ValueError, TypeError):
        return _failure("invalid_response")


def advance_spend(balance: dict, state: dict, now: datetime) -> tuple[dict, dict]:
    """Pure injectable tracking; increases rebase against the LAST reading.

    Sum observed decreases across segments; increases must not erase earlier
    decreases. This is credit consumption (including grants), not cash paid or
    an invoice. Top-up/expiry/usage between samples cannot be separated.
    No FX conversion or RMB price estimate is made.
    """
    if not balance.get("ok"):
        return state, {}
    total, currency = balance["total"], balance["currency"]
    today, at = now.date().isoformat(), int(balance.get("observed_at", now.timestamp()))
    opening, last = _number(state.get("opening")), _number(state.get("last"))
    same_day = state.get("date") == today and state.get("currency") == currency \
        and opening is not None and last is not None
    rebased = False
    if not same_day:
        state = {"date": today, "currency": currency, "opening": total, "last": total,
                 "opening_at": at, "tracking_since": at, "spent": 0.0, "rebases": 0}
        rebased = True
    else:
        state = dict(state)
        # Migrate original state.json, preserving its known decrease.
        spent = _number(state.get("spent"))
        if spent is None:
            spent = round(max(0.0, opening - last), 4)
        state.setdefault("tracking_since", state.get("opening_at", at))
        state.setdefault("rebases", 0)
        change = round(total - last, 4)
        state["spent"] = round(spent + max(0.0, -change), 4)
        if change > 0:
            state.update(opening=total, opening_at=at, rebases=state["rebases"] + 1)
            rebased = True
        # Self-heal a lost segment: with no recharge observed, the accumulator
        # must equal opening - total. Anything smaller means a concurrent writer
        # replaced state.json with an older copy, so trust the balance delta
        # instead of the (incomplete) sum of sampled drops.
        if not state["rebases"] and state["opening"] - total > state["spent"] + 0.005:
            state["spent"] = round(max(0.0, state["opening"] - total), 4)
        # Unchanged balance must not trigger a disk write.
        if change != 0:
            state.update(last=total, last_at=at)
    result = {"spend_ok": True, "opening": state["opening"],
              "spent_today": state["spent"], "opening_at": state["opening_at"],
              "tracking_since": state["tracking_since"], "rebases": state["rebases"],
              "rebased": rebased}
    return state, result


# Official DeepSeek RMB price list, 元 per 1M tokens, stored as (空闲, 高峰).
# 来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing —— 核对日期 2026-11-02。
# 高峰时段＝北京时间（UTC+8）周一至周五 09:00-12:00、14:00-18:00，法定节假日除外；
# 其余时间为空闲时段，单价是高峰的一半。这里不引入任何汇率——只用官方人民币价。
CNY_PRICES = {
    "deepseek-flash": {"hit": (0.02, 0.04), "miss": (1.0, 2.0), "out": (4.0, 8.0)},
    "deepseek-v4-pro": {"hit": (0.15, 0.30), "miss": (4.5, 9.0), "out": (13.5, 27.0)},
}
BEIJING = timezone(timedelta(hours=8))


def is_peak(at: datetime) -> bool:
    local = at.astimezone(BEIJING)
    if local.weekday() >= 5:  # 周六周日全天空闲
        return False
    return 9 <= local.hour < 12 or 14 <= local.hour < 18


def _peak_overlap(start: float, end: float) -> float:
    """Fraction of the [start, end] window that sits inside peak hours.

    Usage rows carry only first_seen/last_seen, so a session straddling 12:00 or
    18:00 is priced pro-rata by wall-clock overlap: that row's tokens are assumed
    spread evenly across its window. Coarse, but much closer than pricing a whole
    day at one tier. Chinese public holidays are treated as weekdays — the error
    only affects the tier of the handful of days involved.
    """
    if end <= start:
        return 1.0 if is_peak(datetime.fromtimestamp(start, tz=timezone.utc)) else 0.0
    total = end - start
    peak = 0.0
    day = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(BEIJING).date()
    last_day = datetime.fromtimestamp(end, tz=timezone.utc).astimezone(BEIJING).date()
    while day <= last_day:
        if day.weekday() < 5:
            for begin, finish in ((9, 12), (14, 18)):
                w_start = datetime.combine(day, dtime(begin), tzinfo=BEIJING).timestamp()
                w_end = datetime.combine(day, dtime(finish), tzinfo=BEIJING).timestamp()
                peak += max(0.0, min(end, w_end) - max(start, w_start))
        day += timedelta(days=1)
    return min(1.0, peak / total) if total else 0.0


def estimate_cny(rows, at=None) -> dict:
    """RMB estimate priced at the tier each usage row actually fell in.

    ``rows`` is (model, input, output, cache_read, first_seen, last_seen).
    A row with no span uses the tier of that instant. ``low``/``high`` keep the
    all-off-peak / all-peak bounds so the residual uncertainty stays visible;
    ``cny`` is the time-weighted number the UI shows.
    """
    at = at or datetime.now().astimezone()
    low = high = weighted = 0.0
    seconds = peak_seconds = 0.0
    unpriced = []
    for model, tokens_in, tokens_out, cache_read, first_seen, last_seen in rows:
        price = CNY_PRICES.get(model)
        if price is None:
            unpriced.append(model)
            continue
        cost = {}
        for tier in (0, 1):
            cost[tier] = ((cache_read or 0) * price["hit"][tier]
                          + (tokens_in or 0) * price["miss"][tier]
                          + (tokens_out or 0) * price["out"][tier]) / 1_000_000
        if not first_seen or not last_seen:
            # Older rows (or a schema without the window columns) cannot be
            # placed in time; bill them at the tier in force right now.
            share = 1.0 if is_peak(at) else 0.0
        else:
            share = _peak_overlap(first_seen, last_seen)
        low += cost[0]
        high += cost[1]
        weighted += cost[0] + (cost[1] - cost[0]) * share
        span = max(0.0, (last_seen or 0) - (first_seen or 0))
        seconds += span
        peak_seconds += span * share
    return {"ok": True, "tier": "peak" if is_peak(at) else "offpeak", "peak": is_peak(at),
            "cny": round(weighted, 2), "low": round(low, 2), "high": round(high, 2),
            "peak_share": round(peak_seconds / seconds, 3) if seconds else None,
            "unpriced": unpriced,
            "source": "api-docs.deepseek.com/zh-cn/quick_start/pricing 2026-11-02"}


def _day_start(day: str) -> float:
    """Local midnight of ``day`` (YYYY-MM-DD) as an epoch second."""
    return datetime.combine(datetime.strptime(day, "%Y-%m-%d").date(), dtime(0)).timestamp()


def _today_activity(conn, day: str) -> dict:
    """session_id -> share of that session's message volume written on ``day``.

    ``messages.token_count`` is not populated on this build, so the share is
    measured by text length: coarse, but it follows when the work actually
    happened instead of spreading it over idle hours.
    """
    start = _day_start(day)
    try:
        rows = conn.execute("""SELECT session_id,
            COALESCE(SUM(CASE WHEN timestamp >= ? AND timestamp < ?
                              THEN length(content) ELSE 0 END), 0),
            COALESCE(SUM(length(content)), 0)
            FROM messages GROUP BY session_id""", (start, start + 86400)).fetchall()
    except sqlite3.Error:
        return {}
    return {sid: (today / total) if total else 0.0 for sid, today, total in rows}


def _summarise_usage(rows, shares, day, now) -> dict:
    """Aggregate today's DeepSeek rows, prorating sessions that began earlier.

    ``session_model_usage`` is cumulative per (session, model), not a daily
    ledger: a session opened yesterday carries yesterday's tokens in the same
    row, and filtering by ``last_seen`` picks it up whole. Summing those rows
    inflated today's cost ~3.7x on 2026-11-03 (¥10.61 estimated against ¥2.85
    actually deducted from the balance). Rows that began before today are
    therefore scaled by the share of the session's message text written today;
    that same day measured ¥2.99 against the ¥2.85 delta.
    """
    start = _day_start(day)
    tokens_in = tokens_out = cache_read = calls = cost = 0.0
    last_seen = None
    sessions = set()
    per_model = {}
    priced = []
    for sid, model, i, o, c, n, usd, first_seen, row_last in rows:
        if first_seen is None or first_seen >= start:
            share = 1.0                      # started today: the row is all today
        else:
            share = shares.get(sid, 0.0)     # carries history: keep today's part
        share = min(1.0, max(0.0, share))
        if share <= 0:
            continue
        tokens_in += i * share
        tokens_out += o * share
        cache_read += c * share
        calls += n * share
        cost += usd * share
        sessions.add(sid)
        if row_last is not None and (last_seen is None or row_last > last_seen):
            last_seen = row_last
        bucket = per_model.setdefault(model, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += i * share
        bucket[1] += o * share
        bucket[2] += c * share
        bucket[3] += usd * share
        priced.append((model, i * share, o * share, c * share,
                       max(first_seen or start, start), row_last))
    models = [{"model": m, "input": round(v[0]), "output": round(v[1]),
               "cache_read": round(v[2]), "estimated_cost_usd": round(v[3], 6)}
              for m, v in sorted(per_model.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))]
    return {"ok": True, "date": day, "scope": "prorated_today",
            "source": "Hermes state.db/session_model_usage",
            "observed_at": int(now.timestamp()), "last_seen": last_seen,
            "sessions": len(sessions), "input": round(tokens_in),
            "output": round(tokens_out), "cache_read": round(cache_read),
            "calls": round(calls), "tokens": round(tokens_in + tokens_out),
            "estimated_cost_usd": round(cost, 6),
            "estimated_cost_cny": estimate_cny(priced, now.astimezone()),
            "models": models}


class UsageService:
    """One instance per process; injection keeps tests off live data."""

    def __init__(self, *, db_path=None, state_path=None, now=None, monotonic=None,
                 key_reader=None, balance_loader=None):
        self.db_path, self.state_path = db_path, state_path
        self.now = now or (lambda: datetime.now().astimezone())
        self.monotonic = monotonic or time.monotonic
        self.key_reader, self.balance_loader = key_reader or api_key, balance_loader or _request_balance
        self._balance = self._usage = self._state = None
        # (mtime_ns, size) of the state.json copy cached above; a mismatch means
        # another writer moved the file and our cache is stale.
        self._state_sig = None
        self._balance_at = self._usage_at = 0.0
        self._balance_day = self._usage_day = None
        self._balance_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._summary_lock = threading.Lock()

    def fetch_balance(self, force=False) -> dict:
        with self._balance_lock:
            day = self.now().date().isoformat()
            if not force and self._balance is not None and day == self._balance_day \
                    and self.monotonic() - self._balance_at < BALANCE_TTL_SECONDS:
                return dict(self._balance)
            try:
                key = self.key_reader()
            except (OSError, UnicodeError):
                value = _failure("key_unreadable")
            else:
                value = self.balance_loader(key) if key else _failure("no_api_key")
            value = {**value, "observed_at": int(self.now().timestamp())}
            self._balance, self._balance_at, self._balance_day = value, self.monotonic(), day
            return dict(value)

    def _state_path(self) -> Path:
        # Override lets API smoke tests persist ONLY inside the plugin repo.
        return Path(self.state_path or os.environ.get("DEEPSEEK_USAGE_STATE_FILE") or
                    hermes_home() / "plugin-data" / "deepseek-usage" / "state.json")

    def track_spend(self, balance: dict) -> dict:
        if not balance.get("ok"):
            return {}
        with self._state_lock:
            path = self._state_path()
            try:
                st = path.stat()
                sig = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                sig = None
            except OSError:
                return {"spend_ok": False, "spend_error": ERROR_MESSAGES["state_io"]}
            if self._state is None or sig != self._state_sig:
                # Another writer (a replaced gateway process, the refresh
                # endpoint) may have moved the file. Serving a cached copy past
                # that point silently drops a whole spending segment.
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                    self._state = state if isinstance(state, dict) else {}
                except (FileNotFoundError, ValueError, UnicodeError):
                    self._state = {}
                except OSError:
                    return {"spend_ok": False, "spend_error": ERROR_MESSAGES["state_io"]}
                self._state_sig = sig
            state, result = advance_spend(balance, self._state, self.now())
            if state != self._state:
                temporary = None
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                     prefix=".state-", suffix=".tmp", delete=False) as f:
                        temporary = Path(f.name)
                        json.dump(state, f, ensure_ascii=False, allow_nan=False)
                    temporary.replace(path)
                except OSError:
                    return {"spend_ok": False, "spend_error": ERROR_MESSAGES["state_io"]}
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass  # A cleanup failure must not hide independent usage.
                self._state = state
                try:
                    st = path.stat()
                    self._state_sig = (st.st_mtime_ns, st.st_size)
                except OSError:
                    self._state_sig = None
            return result

    def usage_for(self, day=None, force=False) -> dict:
        day = day or self.now().date().isoformat()
        with self._usage_lock:
            if not force and self._usage is not None and day == self._usage_day \
                    and self.monotonic() - self._usage_at < USAGE_TTL_SECONDS:
                return self._usage
            db = Path(self.db_path) if self.db_path is not None else hermes_home() / "state.db"
            where = """FROM session_model_usage
                WHERE (billing_provider = 'deepseek' OR model LIKE 'deepseek%')
                  AND date(last_seen, 'unixepoch', 'localtime') = ?"""
            try:
                # Per-TTL connections avoid cross-thread/replaced-DB hazards.
                # sqlite's own context manager does NOT close the connection.
                with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
                    conn.execute("BEGIN")  # Consistent read snapshot; no writes.
                    try:
                        rows = conn.execute("""SELECT session_id, model,
                            COALESCE(input_tokens, 0), COALESCE(output_tokens, 0),
                            COALESCE(cache_read_tokens, 0), COALESCE(api_call_count, 0),
                            COALESCE(estimated_cost_usd, 0), first_seen, last_seen """ + where, (day,)).fetchall()
                    except sqlite3.Error:
                        # Older schema without the window columns: nothing to prorate.
                        rows = [(sid, model, i, o, c, n, usd, None, last)
                                for sid, model, i, o, c, n, usd, last in conn.execute("""SELECT session_id, model,
                            COALESCE(input_tokens, 0), COALESCE(output_tokens, 0),
                            COALESCE(cache_read_tokens, 0), COALESCE(api_call_count, 0),
                            COALESCE(estimated_cost_usd, 0), last_seen """ + where, (day,)).fetchall()]
                    shares = _today_activity(conn, day)
                value = _summarise_usage(rows, shares, day, self.now())
            except sqlite3.Error as exc:
                # sqlite_errorcode is unavailable on older system Python versions.
                code = "no_state_db" if "unable to open" in str(exc) else "sqlite"
                value = _failure(code, date=day, observed_at=int(self.now().timestamp()))
            self._usage, self._usage_day, self._usage_at = value, day, self.monotonic()
            return value

    def summary(self, force=False, day=None) -> dict:
        # Prevent an old concurrent sample from being tracked after a newer one.
        with self._summary_lock:
            balance = self.fetch_balance(force)
            spend = self.track_spend(balance)
            today = self.usage_for(day, force)
            return {"ok": bool(balance.get("ok") or today.get("ok")),
                    "generated_at": int(self.now().timestamp()),
                    "balance": {**balance, **spend}, "today": today,
                    "currency": balance.get("currency")}


_service = UsageService()


def fetch_balance(force=False) -> dict:
    return _service.fetch_balance(force)


def track_spend(balance) -> dict:
    return _service.track_spend(balance)


def usage_for(day=None, force=False) -> dict:
    return _service.usage_for(day, force)


def summary(force=False, day=None) -> dict:
    return _service.summary(force, day)


if __name__ == "__main__":
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
