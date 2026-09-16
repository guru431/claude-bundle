"""The LLM layer of cron/hooks/utils.py: how a failure is classified, how long
the circuit breaker keeps a provider out, and the promise `local` makes.

Everything here is offline. Where a test is about what the dispatcher DECIDES,
`requests` is a stub module; the one test about what `requests` itself would DO
with a local call — proxies, redirects — uses the real library with its
transport patched out, so no socket is opened. Name lookups are faked too, and
the breaker runs on a fake clock.

Run: pytest tests/test_llm_layer.py -q
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sys
import time
import types
from pathlib import Path

import pytest


def _load_utils(bundle: Path, name: str):
    """Import the COPIED cron/hooks/utils.py, so BUNDLE_ROOT lands in tmp.

    A fresh module per call is also a fresh PROCESS as far as the breaker is
    concerned: its in-memory state starts empty and is read back from disk.
    """
    spec = importlib.util.spec_from_file_location(
        name, bundle / "cron" / "hooks" / "utils.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text
        self.headers = {}

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _stub_requests(monkeypatch, response: _Resp) -> list:
    """A fake `requests` whose post() always returns `response`; returns the calls."""
    calls = []

    def post(url, **kw):
        calls.append(url)
        return response

    fake = types.ModuleType("requests")
    fake.post = post
    monkeypatch.setitem(sys.modules, "requests", fake)
    return calls


@pytest.fixture()
def clock(monkeypatch):
    """time.time() under the test's control — no latch here depends on when the
    suite happens to run."""
    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(time, "time", lambda: now["t"])
    return now


def _depleted(bundle: Path) -> dict:
    path = bundle / "cron" / "state" / "depleted.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


# ── A local-only call cannot be carried off the box by `requests` ────────────

def test_a_local_call_ignores_proxy_variables_and_redirects(cron_copy: Path, monkeypatch):
    """`local` promises the transcript never leaves the machine, and the URL
    check alone did not keep that promise. With HTTP_PROXY set and no NO_PROXY,
    `requests` sent a POST to 127.0.0.1 through the proxy host, body and all;
    and it followed a 307 from a local reverse proxy by re-sending the same body
    to the Location. Reproduced against the real library before the fix."""
    requests = pytest.importorskip("requests")
    # Windows environment names are case-insensitive: clear first, then set.
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy",
                 "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "any-local-model")
    u = _load_utils(cron_copy, "utils_local_transport")
    monkeypatch.setattr(u.time, "sleep", lambda _s: None)
    sent = []

    def fake_send(self, request, **kw):
        sent.append((request.url, dict(kw.get("proxies") or {})))
        resp = requests.Response()
        resp.status_code = 307
        resp.headers["Location"] = "https://collector.example.invalid/v1/chat/completions"
        resp.url, resp.request, resp._content = request.url, request, b""
        return resp

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    res = u._llm_openai_compat("local", "TRANSCRIPT")

    assert [url for url, _ in sent] == ["http://127.0.0.1:11434/v1/chat/completions"], \
        f"the redirect was followed — the transcript went to: {sent}"
    assert not any(sent[0][1].get(k) for k in ("http", "https", "all")), \
        f"a local-only call went through the environment's proxy: {sent[0][1]}"
    assert (res.text, res.kind) == (None, "config"), \
        "a redirect the same URL gives every time is configuration"


# ── Whose fault a status code is decides whether it ever quarantines ─────────

@pytest.mark.parametrize("status,kind", [
    (401, "config"),         # revoked key
    (404, "config"),         # a typo in *_MODEL or *_BASE_URL
    (405, "config"),         # a base URL that is not an API
    (400, "deterministic"),  # this payload
    (413, "deterministic"),
    (422, "deterministic"),
    (408, "transient"),
    (520, "transient"),      # a 5xx the backoff does not cover
])
def test_an_http_error_is_classified_by_whose_fault_it_is(cron_copy: Path, monkeypatch,
                                                          status: int, kind: str):
    """Every non-200 that was not 402/403/429/5xx used to be `deterministic`,
    which is the one kind WIKI_RETRY_LIMIT counts. A pinned provider with a
    revoked key (401) or a mistyped model (404) failed every source that way, so
    after three nights all of them were quarantined and marked done — and stayed
    done once the key was fixed. Only a failure about the PAYLOAD may count."""
    u = _load_utils(cron_copy, f"utils_status_{status}")
    monkeypatch.setenv("DEEPSEEK_KEY", "unit-test-placeholder")
    calls = _stub_requests(monkeypatch, _Resp(status, text="nope"))

    res = u._llm_openai_compat("deepseek", "hi")

    assert (res.text, res.kind) == (None, kind)
    assert len(calls) == 1, "a status that is not retried was retried"


def test_a_refused_key_latches_but_a_refused_override_model_does_not(cron_copy: Path,
                                                                    monkeypatch, clock):
    """401 is about the key, the same for every task tonight, so it latches like
    a 402. Unless the call carried a per-call `model` override: a gateway also
    answers 401 for a model its route does not serve, and one job's stronger
    model must not take the provider away from all the others."""
    u = _load_utils(cron_copy, "utils_401")
    monkeypatch.setenv("DEEPSEEK_KEY", "unit-test-placeholder")
    calls = _stub_requests(monkeypatch, _Resp(401, text="invalid api key"))

    assert u._llm_openai_compat("deepseek", "hi", model="other-model").kind == "config"
    assert not u._is_depleted("deepseek"), "an override model's 401 latched the provider"

    assert u._llm_openai_compat("deepseek", "hi").kind == "config"
    assert u._llm_openai_compat("deepseek", "hi again").kind == "config"
    assert len(calls) == 2, "the refused key was sent again instead of latched"
    assert _depleted(cron_copy)["deepseek"]["reason"] == "401"


# ── How long the circuit breaker keeps a provider out ────────────────────────

def test_each_latch_keeps_its_own_timestamp(cron_copy: Path, clock):
    """Every write used to stamp EVERY row with `now`, so each refusal extended
    the others: deepseek/402 at 02:00, opencode/429 at 04:00 and deepinfra/503 at
    05:00 kept DeepSeek dark until 11:00 instead of 08:00."""
    u = _load_utils(cron_copy, "utils_latch_ts")
    t0 = clock["t"]
    u.mark_depleted("deepseek", "402")
    clock["t"] = t0 + 2 * 3600
    u.mark_depleted("opencode", "429")
    clock["t"] = t0 + 3 * 3600
    u.mark_depleted("deepinfra", "503")

    assert _depleted(cron_copy)["deepseek"]["ts"] == t0, \
        "a later refusal moved an earlier provider's latch"
    clock["t"] = t0 + 6 * 3600 + 60
    assert not _load_utils(cron_copy, "utils_latch_ts_next")._is_depleted("deepseek"), \
        "DeepSeek was still out six hours after its own 402"


def test_a_transient_latch_lasts_minutes_and_a_config_one_hours(cron_copy: Path, clock):
    """Exhausted 429/5xx retries latched for six hours, like a spent balance: one
    bad gateway minute at night handed every later payload to the next provider
    in the chain. Waiting is what fixes a transient failure, so it is thirty
    minutes — inside a running process too, since a compile outlasts that."""
    u = _load_utils(cron_copy, "utils_latch_kind")
    t0 = clock["t"]
    u.mark_depleted("opencode", "429")
    u.mark_depleted("deepseek", "402")

    clock["t"] = t0 + 10 * 60
    assert u._is_depleted("opencode"), "the transient latch did not hold at all"
    clock["t"] = t0 + 31 * 60
    assert not u._is_depleted("opencode"), "the running process kept a stale latch"
    nxt = _load_utils(cron_copy, "utils_latch_kind_next")
    assert not nxt._is_depleted("opencode")
    assert nxt._is_depleted("deepseek"), "a spent balance does not refill in half an hour"


def test_a_local_latch_is_short_and_never_written_down(cron_copy: Path, clock):
    """A local server that stops answering is restarting. Persisted for six hours,
    a ten-second Ollama restart silenced it for every task of the night; the
    backoff the shared file saves the next process is five seconds."""
    u = _load_utils(cron_copy, "utils_latch_local")
    t0 = clock["t"]
    u.mark_depleted("local", "503")

    assert "local" not in _depleted(cron_copy), "a local latch reached depleted.json"
    assert not _load_utils(cron_copy, "utils_latch_local_next")._is_depleted("local")
    clock["t"] = t0 + 60
    assert u._is_depleted("local"), "the running process forgot the latch at once"
    clock["t"] = t0 + 6 * 60
    assert not u._is_depleted("local")


def test_a_depleted_file_from_the_previous_version_still_latches(cron_copy: Path, clock):
    """The file keeps its shape — {provider: {ts, reason}} — and only the TTL is
    derived differently, so what a deployed machine already holds is honoured
    rather than thrown away or tripped over."""
    state = cron_copy / "cron" / "state"
    state.mkdir(parents=True, exist_ok=True)
    t0 = clock["t"]
    (state / "depleted.json").write_text(json.dumps({
        "deepseek": {"ts": t0 - 3600, "reason": "402"},
        "opencode": {"ts": t0 - 3600, "reason": "429"},
        "deepinfra": "not a row",
        # json.loads reads this as float('inf'): out for good, and a crash in
        # config_report, if it were taken at its word.
        "local": {"ts": float("inf"), "reason": "402"},
    }), encoding="utf-8")

    u = _load_utils(cron_copy, "utils_old_file")
    assert u._is_depleted("deepseek")
    assert not u._is_depleted("opencode"), "an hour-old 429 is over under the new TTL"
    assert not u._is_depleted("deepinfra")
    assert not u._is_depleted("local"), "a timestamp in the future latched for good"


# ── A name is only as local as what it resolves to ───────────────────────────

@pytest.mark.parametrize("url,addresses,allowed,expected", [
    ("http://localhost:11434/v1", ["127.0.0.1", "::1"], "", True),
    # A resolver without RFC 6761, or a hosts-file line, sends the name away.
    ("http://llm.localhost:11434/v1", ["203.0.113.7"], "", False),
    ("http://localhost:11434/v1", ["127.0.0.1", "203.0.113.7"], "", False),
    ("http://llm.localhost:11434/v1", None, "", False),          # does not resolve
    ("http://llm.localhost:11434/v1", ["::ffff:127.0.0.1"], "", True),
    # A LAN box by IP, allowed on purpose (TEST-NET-2, RFC 5737).
    ("http://198.51.100.20:11434/v1", None, "198.51.100.20", True),
    ("https://api.example.com/v1", None, "", False),
])
def test_a_local_endpoint_is_checked_by_what_it_resolves_to(cron_copy: Path, monkeypatch,
                                                            url, addresses, allowed, expected):
    """`localhost` and `*.localhost` were accepted on spelling. And an IP literal
    was answered by its loopback check alone, so a LAN box named by address in
    LOCAL_LLM_ALLOWED_HOSTS was refused all the same. Only the localhost names
    are looked up: anything else is allowed by name or refused without a query."""
    monkeypatch.setenv("LOCAL_LLM_ALLOWED_HOSTS", allowed)
    u = _load_utils(cron_copy, "utils_local_endpoint")
    lookups = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        lookups.append(host)
        if addresses is None:
            raise socket.gaierror(socket.EAI_NONAME, "name does not resolve")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
                 6, "", (a, 0, 0, 0) if ":" in a else (a, 0)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert u._is_local_endpoint(url) is expected
    host = url.split("//", 1)[1].split(":", 1)[0].split("/", 1)[0]
    looked_up = host == "localhost" or host.endswith(".localhost")
    assert lookups == ([host] if looked_up else []), \
        f"unexpected name lookups: {lookups}"
