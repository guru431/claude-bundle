# LLM routing

The bundle has two distinct places where LLM choice matters:

1. **`scripts/claude-switch.ps1`** — switches the **Claude Code session
   itself** between providers (Anthropic / DeepSeek / MiniMax / OpenCode
   Go / Ollama / CCR). One mode active at a time, writes to
   `<project>/.claude/settings.local.json::env`.

2. **`home-claude/cron/hooks/utils.py::llm_call()`** — calls an LLM
   from **background scripts** (wiki compilers, memory updaters,
   healthcheck). These should NOT consume your Claude subscription
   silently — they use a cheap PAYG provider with a fallback chain.

## Why two systems

The Claude Code session is **interactive**. You want a model that's good
at coding, tolerant of long context, and aligned with the tool-use
protocol. Anthropic Claude (default) and DeepSeek V4 (cheaper) are both
solid; OpenCode Go gives you access to a buffet of other models through
one billing relationship.

Cron LLM calls are **batch**. They process JSONL transcripts, write wiki
pages, and run maintenance. You want the cheapest reliable model that
doesn't burn through your Claude subscription. DeepSeek V4-Flash is hard
to beat on that axis — its published rate has only moved down since this
bundle was written, so check the
[current pricing](https://api-docs.deepseek.com/quick_start/pricing/)
rather than trusting a number frozen in a doc (as of 2026-07 it was in
the low tens of cents per M input tokens).

## claude-switch modes — when to use what

| Mode | When | Cost | Notes |
|---|---|---|---|
| `anthropic` | default | OAuth subscription or API per-token | best alignment, native tool-use |
| `deepseek` | budget mode, big sessions | PAYG, ~$6/mo at the author's volume | identical surface to Anthropic; works seamlessly |
| `minimax` | MiniMax direct (M3 default, M2.7 legacy) | direct billing | direct latency lower than via OCG |
| `opencode` | OCG models over the Anthropic surface | OCG subscription | the OCG Anthropic-compatible endpoint exposes `minimax-m3` / `qwen3.7-max` (qwen is messages-only — it 401s on the OpenAI-compat route, so it works here but not via CCR) |
| `ollama` | local/LAN models, offline work | free (your hardware) | Ollama serves the Anthropic `/v1/messages` API natively — no proxy; host:port from `OLLAMA_HOST` (default `127.0.0.1:11434`); a dummy `ANTHROPIC_AUTH_TOKEN=ollama-local` overrides any stored OAuth session |
| `ccr` | routing many models through one proxy | depends on the underlying provider | needs the [Claude Code Router](https://github.com/musistudio/claude-code-router) running |

Each mode reads its credential from `.env` (see
`config/llm-providers.example.env`): `anthropic` → `ANTHROPIC_API_KEY`
(optional — OAuth usually suffices), `deepseek` → `DEEPSEEK_KEY`,
`minimax` → `MINIMAX_API_KEY`, `opencode` → `OPENCODE_GO_API_KEY`,
`ollama` → `OLLAMA_HOST` (no key), `ccr` → `CCR_API_KEY` plus an optional
`CCR_HOST` (default `127.0.0.1:3456`).

The CCR mode is the most powerful — one proxy, 12 models, same Anthropic-
compatible surface. But it needs you to run `ccr` somewhere (locally or
on a LAN host). The script auto-probes the CCR port and tries to launch
`ccr start` if `ccr.cmd` is on the local machine.

### Why ANTHROPIC_AUTH_TOKEN, not ANTHROPIC_API_KEY

For DeepSeek and CCR, the bundle sets `ANTHROPIC_AUTH_TOKEN`. The reason
is subtle: when you're signed in to Anthropic via `claude /login`, your
OAuth `accessToken` lives in `.credentials.json`. Claude Code **prefers
OAuth over `ANTHROPIC_API_KEY`**, so setting just `ANTHROPIC_API_KEY` to
a non-Anthropic key produces 401s — the OAuth token wins and gets sent
to the wrong endpoint.

`ANTHROPIC_AUTH_TOKEN` is the override that wins over OAuth, sent as
`Authorization: Bearer <key>`. DeepSeek and CCR both accept it.
OpenCode Go's Anthropic endpoint is an exception — it wants
`x-api-key`, so the `opencode` mode uses `ANTHROPIC_API_KEY` instead
and explicitly clears the env in `anthropic` mode to fall back to OAuth.

## utils.py::llm_call — fallback chain

```
WIKI_LLM_PROVIDER env var:
  "deepseek"  →  DeepSeek V4-Flash  →  OpenCode Go  →  DeepInfra  →  None
  "opencode"  →  OpenCode Go (mimo-v2.5-pro)  →  None
  "deepinfra" →  DeepInfra (deepseek-ai/DeepSeek-V3.1)  →  None
  "claude"    →  claude CLI (sonnet)  →  None  [opt-in only]
  "mock"      →  canned text from $WIKI_LLM_MOCK_RESPONSE  →  "[]"  [tests/CI]
```

`mock` (like `claude`) is special-cased in `llm_call()`, not a `PROVIDERS`-table
entry — it needs no key, endpoint or model. It returns the verbatim contents of
the file named by `WIKI_LLM_MOCK_RESPONSE`, letting `tests/test_pipeline.py`
drive the whole flush→compile→index→lint flow offline (see that test).

### One account, one queue

Every provider call except `mock` goes through a cross-process lock at
`cron/state/.llm.lock`. All the scheduled tasks share a single provider account,
so two runs overlapping is self-inflicted rate limiting: the second one collects
HTTP 429s and the run it belongs to fails. Spacing the triggers apart in
`registry.yaml` does not solve it — run durations drift, and a compile that
usually takes 20 minutes occasionally takes 90 and rolls into the next task's
window. So the serialization lives in the call, not the schedule.

It is fail-open on purpose: no slot within `WIKI_LLM_LOCK_WAIT` (900s) and the
call proceeds anyway — a 429 beats a silently skipped nightly job — while a lock
older than `WIKI_LLM_LOCK_STALE` (1800s) is treated as abandoned by a killed
process. A waiter that timed out never removes the holder's lock.

The default is `deepseek`. **Claude is never the silent fallback** — the
chain returns `None` (and the calling script logs an error) rather than
silently chew through your Claude subscription. If a wiki compile fails
because DeepSeek is down, that's a Telegram alert, not a $5 surprise.

The chain order lives in `utils.py::DEFAULT_CHAIN`. Precisely which values
select it:

- `WIKI_LLM_PROVIDER` **unset, empty, or `deepseek`** — the whole chain.
  `deepseek` is the chain's *name*, not a way to pin DeepSeek alone: the two
  spellings are indistinguishable to the code, and `INSTALL.md` step 9 has
  always suggested writing it out.
- **any other registry key** (`opencode`, `deepinfra`, `local`, `claude`,
  `mock`) — that provider only, no fallback. An explicit choice of a
  non-default provider must not silently route elsewhere.

So to run DeepSeek and *nothing else*, the switch is
`WIKI_OFFBOX_FALLBACK=0`, which suppresses every off-box step of the chain —
not a `WIKI_LLM_PROVIDER` value. Two gateways sit behind the primary rather
than one: with a single fallback, both being down at once leaves the pipeline
dark for a whole night.

This public default (DeepSeek direct primary, OpenCode Go fallback) is
deliberate because DeepSeek PAYG is universally available with no
subscription gate; your own routing policy may legitimately invert or
replace it by setting `WIKI_LLM_PROVIDER` and editing the `PROVIDERS`
table below.

### Provider registry — the single source of truth (cron side)

All cron-side provider config lives in **one** table,
`utils.py::PROVIDERS` — env-var names, endpoints, default models, and
the call parameters (`max_tokens` / `temperature` / `max_retries`). The
module-level constants (`DEEPSEEK_*`, `OPENCODE_*`) are derived from it.
An unknown `WIKI_LLM_PROVIDER` value is rejected loudly (stderr warning,
fallback to `deepseek`) instead of silently routing to the default
branch. This table below mirrors the registry — keep the two in sync
(and the `.env` template too):

| Provider key | Key env (first non-empty wins) | Base URL | Default model | Model override env |
|---|---|---|---|---|
| `deepseek` | `DEEPSEEK_KEY` | `https://api.deepseek.com/v1` (`DEEPSEEK_BASE_URL`) | `deepseek-v4-flash` | `DEEPSEEK_MODEL` |
| `opencode` | `OPENCODE_GO_API_KEY`, `OPENCODE_GO_KEY` | `https://opencode.ai/zen/go/v1` | `mimo-v2.5-pro` | `OPENCODE_GO_MODEL` |
| `deepinfra` | `DEEPINFRA_KEY` | `https://api.deepinfra.com/v1/openai` (`DEEPINFRA_BASE_URL`) | `deepseek-ai/DeepSeek-V3.1` | `DEEPINFRA_MODEL` |
| `local` | `LOCAL_LLM_KEY` (optional) | `http://localhost:11434/v1` (`LOCAL_LLM_BASE_URL`) | *(none — must be set)* | `LOCAL_LLM_MODEL` |

Every row is served by one adapter, `_llm_openai_compat()`, because all
three speak the same OpenAI-compatible `/chat/completions`. It used to be
a function per provider, and the 402/429/529 contract drifted between the
copies. To add a provider for cron use: add a row to `PROVIDERS`, add the
key name to `config/llm-providers.example.env`, and add a row here — no
new caller unless the provider speaks a different protocol.

### Local-only runs

`WIKI_LLM_PROVIDER=local` points the pipeline at any OpenAI-compatible
server on your own machine (Ollama, llama.cpp, LM Studio, vLLM), so
transcripts never leave the box.

Two separate mechanisms, worth not confusing:

- **`local` verifies its own endpoint.** The row is declared `offbox:
  False`, and that promise is checked rather than assumed: the host in
  `LOCAL_LLM_BASE_URL` must be loopback/`localhost`, or the call is
  REFUSED and nothing is sent. A typo pointing at a remote host cannot
  quietly turn "local-only" into "shipped to a stranger". To use a
  deliberately non-loopback but trusted server (an inference box on your
  LAN), name its host in `LOCAL_LLM_ALLOWED_HOSTS` (comma-separated) —
  making it an explicit decision instead of an unnoticed URL.
- **`WIKI_OFFBOX_FALLBACK=0` controls the FALLBACK CHAIN, nothing else.**
  The default `deepseek` chain falls back to the OpenCode Go gateway when
  DeepSeek fails, which would push a prompt off-box *because* the primary
  broke; this flag forbids that. It is not a DLP switch: it does not
  restrict where an explicitly chosen provider points, and it does not
  stop the FIRST provider in the chain (DeepSeek — off-box) from being
  called. An explicit `local` never falls back to anything in the first
  place — it is that provider alone, or `None`.
- **`WIKI_ALLOW_OFFBOX=0` is the DLP switch.** It refuses every provider
  whose registry row says `offbox: True`, on every call including the
  first, with the same "nothing was sent" refusal the local-only endpoint
  check uses. That makes "fully local" one variable instead of
  `WIKI_LLM_PROVIDER=local` plus a belief about when the chain fires.
  Pair it with a local server; with none configured the LLM phases no-op
  rather than quietly reaching out. `bundle-status.py` prints the flag,
  and so does the `[llm] provider=…` line of every run.

The active policy and the resolved base URL are printed once per run in
the `[llm] provider=…` line.

The `local` row deliberately ships **no default model** — an unset
`LOCAL_LLM_MODEL` fails loudly instead of quietly calling whatever
happens to be named in someone else's default.

If you want claude as a one-off — `WIKI_LLM_PROVIDER=claude python ...`.
Don't make it the cron default.

### Where the keys come from

`utils.py::_load_dotenv()` reads the `.env` next to the deployed bundle
content — i.e. `~/.claude/.env` after a standard install (the path is
derived as two levels up from `cron/hooks/utils.py`) — at import time
and populates `os.environ` (without overwriting anything already set).
`claude-switch.ps1` reads the same file as its last fallback (after the
process env, the user env, and a `.env` next to the script). That means:

- Cron tasks fired through Task Scheduler — they read from `.env`
  because Task Scheduler doesn't carry your user environment into
  session 0.
- Interactive runs from your terminal — the shell env wins.

The keys themselves never get committed: `.env` is in `.gitignore`.
`config/llm-providers.example.env` is the template (no values).

## Picking a provider for your own deployment

If you have a Claude subscription and **don't** want to spend separately:
- `claude-switch anthropic` — done. Wiki/cron LLM calls become a problem
  though, because you don't want them on the subscription. Either skip
  the wiki/cron components, or sign up for one of the cheap PAYG
  providers below.

If you want cheap + reliable for batch:
- DeepSeek V4-Flash direct PAYG. Illustrative only: ~$6/mo at the author's
  moderate volume, at the rates of the time — your bill depends on your
  session volume and the
  [current rate](https://api-docs.deepseek.com/quick_start/pricing/). Set
  `DEEPSEEK_KEY` in `.env`. Default config picks this up.

If you want one bill for many models:
- OpenCode Go subscription. Flat rate, ~12 models including Kimi K2,
  GLM 5.1, MiniMax M2.7, mimo, Qwen. Set `OPENCODE_GO_API_KEY` in `.env`.

You can mix — e.g. claude-switch on Anthropic for interactive, DeepSeek
in `.env` for wiki/cron. They're independent.

### On a flat-rate subscription, pick the model by quota — not by latency

A flat-rate multi-model plan bills one budget for the whole account, and the
models draw on it at wildly different weights — an order of magnitude or two
between the cheapest and a strong reasoning model is normal. That makes the
obvious benchmark (which model answers fastest) the wrong criterion for the
nightly pipeline, and picking on it has a specific failure mode: the budget is
exhausted mid-month, every later call falls through to the metered fallback,
and the bill arrives from the provider you thought you had replaced. Nothing in
the logs says "you chose wrong" — the pipeline keeps working.

Do the arithmetic before switching the model the cron uses:

1. Count the pipeline's calls per month (`cron/logs/provider_attempts_*.jsonl`
   records one line per attempt).
2. Ask the provider how many calls of the candidate model the plan covers, or
   derive it from the published weights.
3. Divide. If the pipeline needs more than a fraction of the budget, the model
   is unaffordable regardless of how fast it is.

The pattern that works: cheap high-quota model for the bulk nightly work,
expensive model reserved for the few places where its quality actually changes
the output (a weekly code review, an analysis whose result somebody reads). A
slow model in a nightly window costs nothing but wall clock — which is what the
window is for.

Two things worth watching while you are there: HTTP 403 is latched for the rest
of the run (a model not enabled for your account, or a WAF), and so is a 402 or
an exhausted-cap 429 — see `_DEPLETED_PROVIDERS` in `utils.py`. Without that
latch every remaining call of the batch pays another round trip to a door that
is known to be shut.
