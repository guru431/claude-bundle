# Test policy — the full text

`~/.claude/CLAUDE.md` § Test policy carries the short form. This is the long
one, with the reason behind each rule. `pytest.ini` in the bundle repository is
the reference implementation.

An audit across 20 projects found 5 with no tests at all, 8 with no pytest
config, one full run that did not finish in 17 minutes, and a suite that had
been red for two days without anyone noticing. Each rule below exists to
prevent one of those.

1. **Two levels, fast by default.** Bare `pytest` = the fast suite, **60s
   budget**. Anything that reaches the network, a share, a real database, a
   model, hardware or an LLM is marked `integration`; anything run by hand is
   `manual`. Both are excluded through
   `addopts = -m "not integration and not manual"`. A config is mandatory in
   every project that has tests.
2. **The one-second threshold.** A test over a second in the fast suite is
   either fixed or marked `integration`. Mark **by measurement**
   (`--durations`), never by directory name: in one project the tests under
   `tests/unit/` polled real hardware, and one of them took 53 seconds.
3. **No real clock.** `datetime.now()`, `date.today()`, ISO week numbers and
   time zones only through injection or a fake. A test that depends on the
   calendar is green some days and red others — one suite quietly went red on
   even ISO weeks, another exactly 60 days after its fixture was written.
4. **Something other than a person runs them.** CI where a project has it;
   otherwise the bundle's own sweep: `ClaudeTestSweep` (daily, off by default)
   runs the fast suite across every project under `projects_root`, red earns a
   Telegram alert and an entry in that project's `FINDINGS.md`, and
   `ClaudeTestSweepFull` does the same weekly including `integration`. The rule
   is the first sentence — a suite only a human remembers to run is a suite that
   goes red for two days unnoticed, which is what prompted this policy.
5. **"Why does this test exist."** Write one for: (a) a reproduced bug or
   incident, (b) a contract between modules or services, (c) an irreversible
   operation — deletion, deploy, migration, writing to an archive. Do not write
   one for trivial wrappers, combinatorial variations of the same thing, or
   markup details. Weed out the duplicates when refactoring.
6. **A project with no tests gets a minimum, not a suite.** A smoke test on the
   entry point (`--help` / `--dry-run` does not crash) plus a test for its most
   dangerous operation. Mandatory for code that touches infrastructure or
   production.
7. **A time limit per test.** A hung test must not hold the run until whatever
   started it gives up — unbounded, one hang cost tens of minutes per run.
   pytest: `pytest-timeout` in the config, `timeout = 30` for the fast suite; a
   test or level that needs longer gets its own value (`@pytest.mark.timeout(N)`
   or `--timeout=N` in that level's command). Runners other than pytest: a
   timeout per test file.
8. **The rules hold for every runner**, not only pytest: Bash, Pester, xunit and
   JS suites are split into the same fast default level (60 s budget) and a full
   one.
9. **Test commands are declared once.** Three levels per project — targeted
   (one module), fast, full — with runner and interpreter, in the `tests:` key
   of `~/.claude/bundle.local.yaml` (or, without the bundle's sweep, in the
   project's `CLAUDE.md`). The sweep and agents run those commands instead of
   composing their own; a project without an entry is auto-discovered as pytest.

Do not put `--cov` in `addopts`: coverage is measured on demand, not on every run.
