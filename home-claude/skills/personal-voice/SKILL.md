---
name: personal-voice
description: Use when asked to write text "in my voice" / "in my style" — drafting personal correspondence, announcements, posts, internal team messages, formal emails, or replies to colleagues. Picks the right register (email / chat / technical) and applies anti-AI rules on top.
---

# Personal voice skill

You are about to write text **as the user** (not as the Claude assistant).
The user has profiled their own voice from real corpora, separated into
distinct registers. Each register has its own profile file the skill reads
before composing.

This skill is a **template** — you must populate the profile files yourself
before it does anything useful. See the "Setup" section at the bottom.

## Registers

| Register | Profile file (default) | Source corpus example | Use when |
|---|---|---|---|
| **Formal email** | `<voice-root>/profile_email.md` | Outlook Sent items, formal correspondence | Drafting emails, official requests, corporate correspondence, replies to colleagues' emails — anything where you'd write "Dear …" / "Best regards" |
| **Live chat** | `<voice-root>/profile_telegram.md` | Telegram / WhatsApp / Slack DMs | Chat messages, personal correspondence, informal team chat, replies to family / friends |
| **Technical** | `<voice-root>/profile_claude.md` | Prompts to coding assistants, technical task descriptions | Prompts for LLM agents, instructions to coding assistants, technical task descriptions for AI |

These registers DIFFER significantly. Email has greetings/signatures, chat is
informal with slang, technical is imperative and terse. **Do not mix them** —
the user explicitly separated them.

Adjust `<voice-root>` to wherever you store your own profile files. A common
choice is a sibling folder like `~/voice/` or inside the project you own.

## Mandatory steps

1. **Identify the register** the user wants. Three signals:
   - Explicit cue in the request ("письмо" / "email" / "answer the email" → email;
     "telegram" / "chat message" / "tg" → chat; "prompt for an agent" /
     "claude prompt" → technical)
   - Format of the surrounding context (quoted email body? chat snippet?
     code task?)
   - If still ambiguous — ask the user via AskUserQuestion which register fits

2. **Read the chosen profile.** Use the Read tool on the corresponding
   `<voice-root>/profile_<register>.md`.

3. **Read the anti-AI rules.** Use the Read tool on
   `<voice-root>/anti-ai-rules.md` — a universal LLM-cliché blacklist that
   applies on top of all registers.

4. **Compose the draft.** Apply the chosen profile. Then sanity-check
   against `anti-ai-rules.md` and remove any clichés that slipped through.

5. **Show the draft.** Output as a plain code block (so the user can copy
   verbatim) — no commentary above or below unless asked. If you used a
   different register than the user implied, name which one and why.

## Triggers

Activate this skill when the user requests:
- "write as me …" / "draft a letter …" / "reply to N …"
- "in my style" / "in my voice"
- "draft a reply to …"
- drafting for personal correspondence, posts, announcements, internal
  communication, email reply
- explicit `/personal-voice` invocation
- explicit `/personal-voice email` / `/personal-voice tg` / `/personal-voice tech`
  to force a register

## When NOT to apply

- Technical documentation (CLAUDE.md, READMEs, code comments) — there the
  user's normal terse-imperative style already shows through; loading the
  full voice profile is overkill
- Auto-generated reports (cron output, weekly summaries) — these aren't
  their voice, they're system output
- Code itself
- Translation tasks where the source author's voice should be preserved

## Notes

- Profiles reflect the user's **observed** style. If they ask to write in a
  different register (e.g. "formally, for a notary"), follow the explicit
  instruction over the profile — but still drop anti-AI clichés
- If the profile describes a habit that contradicts an explicit instruction,
  ask which to follow
- The three profiles are deliberately separate. Do not synthesize across
  them on your own — the user already decided each register stays distinct

---

## Setup (one-time, before first use)

This skill needs three voice-profile files plus a shared anti-AI rules file.
You provide them. Two ways:

1. **Manual.** Write each profile yourself. ~500–1500 words per register
   covering: typical openings/closings, sentence rhythm, vocabulary they
   gravitate to / avoid, punctuation habits, formality level, emoji use,
   common turns of phrase.

2. **From a corpus.** If you have a body of your own text (sent emails,
   exported chat history, prior LLM prompts), an LLM with a long context
   can summarize the style into a profile. Re-run periodically as your
   corpus grows.

Files needed under your `<voice-root>` (any path that suits you):

```
<voice-root>/
├── profile_email.md
├── profile_telegram.md
├── profile_claude.md
└── anti-ai-rules.md
```

Then update the paths in the "Mandatory steps" section of this skill to
point to your real `<voice-root>`. If you want, you can also commit those
profiles into a private repo — but keep them OUT of any public bundle:
they're a personal artifact.
