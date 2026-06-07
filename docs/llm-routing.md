# LLM routing

The bundle has two distinct places where LLM choice matters:

1. **`scripts/claude-switch.ps1`** — switches the **Claude Code session
   itself** between providers (Anthropic / DeepSeek / MiniMax / OpenCode
   Go / CCR). One mode active at a time, writes to
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
doesn't burn through your Claude subscription. DeepSeek V4-Flash at
~$0.27/M input is hard to beat.

## claude-switch modes — when to use what

| Mode | When | Cost | Notes |
|---|---|---|---|
| `anthropic` | default | OAuth subscription or API per-token | best alignment, native tool-use |
| `deepseek` | budget mode, big sessions | PAYG, ~$6/mo at the author's volume | identical surface to Anthropic; works seamlessly |
| `minimax` | when MiniMax-M2.7 specifically | direct billing | direct latency lower than via OCG |
| `opencode` | trying out a non-MiniMax model briefly | OCG subscription | the OCG Anthropic-compatible endpoint only exposes MiniMax-M2.7 / M2.5 |
| `ccr` | routing many models through one proxy | depends on the underlying provider | needs the [Claude Code Router](https://github.com/modelfusion/claude-code-router) running |

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
  "deepseek"  →  DeepSeek V4-Flash  →  on failure, OpenCode Go  →  None
  "opencode"  →  OpenCode Go (mimo-v2.5-pro)  →  None
  "claude"    →  claude CLI (sonnet)  →  None  [opt-in only]
```

The default is `deepseek`. **Claude is never the silent fallback** — the
chain returns `None` (and the calling script logs an error) rather than
silently chew through your Claude subscription. If a wiki compile fails
because DeepSeek is down, that's a Telegram alert, not a $5 surprise.

### Provider registry — the single source of truth (cron side)

All cron-side provider config lives in **one** table,
`utils.py::PROVIDERS`. The module-level constants (`DEEPSEEK_*`,
`MINIMAX_*`) are derived from it, so there is exactly one place that lists
env-var names, endpoints and default models. This table below mirrors it —
keep the two in sync (and the `.env` template too):

| Provider key | Key env (first non-empty wins) | Base URL | Default model | Model override env |
|---|---|---|---|---|
| `deepseek` | `DEEPSEEK_KEY` | `https://api.deepseek.com/v1` (`DEEPSEEK_BASE_URL`) | `deepseek-v4-flash` | `DEEPSEEK_MODEL` |
| `opencode` | `OPENCODE_GO_API_KEY`, `OPENCODE_GO_KEY` | `https://opencode.ai/zen/go/v1` | `mimo-v2.5-pro` | `OPENCODE_GO_MODEL` |

To add a provider for cron use: add a row to `PROVIDERS`, wire an
`_llm_<name>()` caller into `llm_call()`, add the key name to
`config/llm-providers.example.env`, and add a row here.

If you want claude as a one-off — `WIKI_LLM_PROVIDER=claude python ...`.
Don't make it the cron default.

### Where the keys come from

`utils.py::_load_dotenv()` reads `<bundle-root>/.env` at import time and
populates `os.environ` (without overwriting anything already set). That
means:

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
- DeepSeek V4-Flash direct PAYG. ~$6/mo at moderate volume. Set
  `DEEPSEEK_KEY` in `.env`. Default config picks this up.

If you want one bill for many models:
- OpenCode Go subscription. Flat rate, ~12 models including Kimi K2,
  GLM 5.1, MiniMax M2.7, mimo, Qwen. Set `OPENCODE_GO_API_KEY` in `.env`.

You can mix — e.g. claude-switch on Anthropic for interactive, DeepSeek
in `.env` for wiki/cron. They're independent.
