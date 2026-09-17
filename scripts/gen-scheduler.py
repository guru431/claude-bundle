#!/usr/bin/env python3
"""Emit POSIX scheduler units from the OS-neutral cron/registry.yaml.

The registry describes *what runs when* independently of Windows Task Scheduler.
This generator translates each enabled task into either:

  - systemd  : a <name>.service (Type=oneshot) + <name>.timer  (Linux)
  - launchd  : a com.claude-bundle.<name>.plist                (macOS)

so the full-tier wiki+cron pipeline can run on mac/linux, not just Windows.

Windows-only task kinds (cmd / vbs / exec) or tasks marked `platform: windows`
are skipped with a note — they have no POSIX equivalent. The `logon_type` /
Password machinery is Windows-specific
and irrelevant here (systemd/launchd run under the invoking user).

Usage:
  scripts/gen-scheduler.py --target systemd --install-path ~/.claude --out-dir ./units
  scripts/gen-scheduler.py --target launchd --install-path ~/.claude
  scripts/gen-scheduler.py --target both --all      # include disabled tasks too
  scripts/gen-scheduler.py --check --install-path ~/.claude \\
      --registry ~/.claude/cron/registry.yaml       # installed units vs the registry

Then follow the printed enable instructions (systemctl --user enable --now, or
launchctl load).

--check writes nothing. It generates into a temporary directory and compares
that with the units the init system actually loads (systemd:
$XDG_CONFIG_HOME/systemd/user, else ~/.config/systemd/user; launchd:
~/Library/LaunchAgents — or --units-dir): `new` (not installed), `changed`
(installed, different), `stale` (installed, no longer generated — a removed or
disabled task whose timer still fires) and `unchanged`. Exit 3 on any drift.
Pass the same --install-path / --registry / --all you installed with.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shlex
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"
LAUNCHD_PREFIX = "com.claude-bundle."

# registry weekday -> (systemd 3-letter, launchd 0=Sun..6=Sat)
DOW = {
    "mon": ("Mon", 1), "monday": ("Mon", 1),
    "tue": ("Tue", 2), "tuesday": ("Tue", 2),
    "wed": ("Wed", 3), "wednesday": ("Wed", 3),
    "thu": ("Thu", 4), "thursday": ("Thu", 4),
    "fri": ("Fri", 5), "friday": ("Fri", 5),
    "sat": ("Sat", 6), "saturday": ("Sat", 6),
    "sun": ("Sun", 0), "sunday": ("Sun", 0),
}

# The registry trigger grammar. Single source of truth: scripts/check-registry.py
# imports these to validate registry.yaml, so a task this generator would
# silently skip fails the check instead. Keep in sync with the `trigger:` section
# of home-claude/cron/registry.yaml and Build-XmlTrigger in cron/admin/sync-tasks.ps1.
TRIGGER_DAILY = re.compile(r"Daily (\d{1,2}):(\d{2})")
TRIGGER_WEEKLY = re.compile(r"Weekly (\w+) (\d{1,2}):(\d{2})")
TRIGGER_MONTHLY = re.compile(r"Monthly day=(\d{1,2}) (\d{1,2}):(\d{2})")
TRIGGER_SIMPLE = ("AtLogOn", "AtStartup")


def load_tasks(registry: Path | None = None) -> list[dict]:
    text = (registry or REGISTRY).read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text)["tasks"]
    except Exception:
        print("ERROR: PyYAML is required for gen-scheduler "
              "(pip install pyyaml)", file=sys.stderr)
        raise


def iso_hours(dur: str) -> int | None:
    """PT4H -> 4. Returns None for non-whole-hour durations."""
    m = re.fullmatch(r"PT(\d+)H", dur or "")
    return int(m.group(1)) if m else None


def iso_seconds(dur: str) -> int | None:
    """PT4H / PT30M / P1D -> seconds. None if unparseable."""
    if not dur:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", dur)
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = ((d * 24 + h) * 60 + mi) * 60 + s
    return total or None


def posix_script(task: dict, install_path: str) -> str:
    raw = str(task.get("script", ""))
    raw = raw.replace("<bundle-install-path>", install_path.rstrip("/\\"))
    return raw.replace("\\", "/")


def exec_argv(task: dict, install_path: str, python: str | None = None) -> list[str] | None:
    kind = task.get("kind", "bash")
    script = posix_script(task, install_path)
    extra = [str(a) for a in (task.get("script_args") or [])]
    if kind == "bash":
        return ["/bin/bash", script] + extra
    if kind in ("python", "python_local"):
        # `/usr/bin/env python3` resolves against the init system's PATH
        # (/usr/bin:/bin under systemd --user), not yours: a pyenv or venv
        # interpreter holding PyYAML and requests is simply not found at 02:30.
        # --python pins the one the installer verified.
        return ([python] if python else ["/usr/bin/env", "python3"]) + [script] + extra
    return None  # cmd / vbs / exec — Windows-only, no POSIX equivalent


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart token. systemd splits the command line with
    shell-like quoting rules, and expands '%' as a specifier prefix even
    inside quotes — so '%' must be doubled regardless."""
    return shlex.quote(arg).replace("%", "%%")


def systemd_oncalendar(task: dict) -> tuple[str, str] | None:
    """Return (kind, value) where kind is 'OnCalendar' or 'OnBootSec', or None."""
    trig = str(task.get("trigger", ""))
    rep_raw = str(task.get("repeat_every", "") or "")
    rep_h = iso_hours(rep_raw)
    # A repetition this generator cannot express must not be dropped in silence:
    # the OnCalendar step syntax below only takes whole hours, so PT30M (which
    # check-registry.py accepts, validating via iso_seconds) used to produce a
    # plain once-a-day timer — the same registry line running 48x less often on
    # Linux than on Windows, with nothing printed. Return None; emit_systemd
    # turns that into a `skip` line naming the reason.
    if rep_raw and rep_h is None:
        return None
    m = TRIGGER_DAILY.fullmatch(trig)
    if m:
        h, mi = int(m.group(1)), m.group(2)
        if rep_h:  # every rep_h hours starting at h (systemd step syntax)
            return ("OnCalendar", f"*-*-* {h:02d}/{rep_h}:{mi}:00")
        return ("OnCalendar", f"*-*-* {h:02d}:{mi}:00")
    # Only the Daily branch can carry the step syntax; a Weekly/Monthly trigger
    # with repeat_every would otherwise lose it just as quietly.
    if rep_h and not TRIGGER_DAILY.fullmatch(trig):
        return None
    m = TRIGGER_WEEKLY.fullmatch(trig)
    if m:
        dow = DOW.get(m.group(1).lower())
        if dow:
            return ("OnCalendar", f"{dow[0]} *-*-* {int(m.group(2)):02d}:{m.group(3)}:00")
    m = TRIGGER_MONTHLY.fullmatch(trig)
    if m:
        return ("OnCalendar", f"*-*-{int(m.group(1)):02d} {int(m.group(2)):02d}:{m.group(3)}:00")
    if trig == "AtStartup":
        delay = iso_seconds(task.get("startup_delay", "")) or 60
        return ("OnBootSec", f"{delay}s")
    return None  # AtLogOn and anything else: unsupported here


def emit_systemd(task: dict, install_path: str, out: Path,
                 python: str | None = None) -> str | None:
    name = task["name"]
    argv = exec_argv(task, install_path, python)
    if argv is None:
        return f"skip {name}: kind={task.get('kind')} has no POSIX equivalent"
    sched = systemd_oncalendar(task)
    if sched is None:
        rep = str(task.get("repeat_every", "") or "")
        if rep:
            return (f"skip {name}: repeat_every={rep} with trigger "
                    f"'{task.get('trigger')}' has no systemd equivalent "
                    f"(OnCalendar steps take whole hours on a Daily trigger)")
        return f"skip {name}: trigger '{task.get('trigger')}' unsupported for systemd"
    desc = str(task.get("description", "")).replace("\n", " ")
    exec_line = " ".join(systemd_quote(a) for a in argv)
    service = (
        f"[Unit]\nDescription={desc}\n\n"
        f"[Service]\nType=oneshot\nExecStart={exec_line}\n"
    )
    # timeout_hours is the registry's "kill it if it hangs" contract. Dropping it
    # meant a wedged nightly job on POSIX ran forever, while the same registry on
    # Windows capped it — same declaration, two behaviours. 0/absent = unlimited,
    # matching Build-TaskXml's PT0S.
    timeout_h = task.get("timeout_hours")
    if isinstance(timeout_h, int) and not isinstance(timeout_h, bool) and timeout_h > 0:
        service += f"RuntimeMaxSec={timeout_h * 3600}\n"
    if sched[0] == "OnBootSec":
        timer = (f"[Unit]\nDescription=Timer for {name}\n\n"
                 f"[Timer]\nOnBootSec={sched[1]}\nPersistent=true\n\n"
                 f"[Install]\nWantedBy=timers.target\n")
    else:
        timer = (f"[Unit]\nDescription=Timer for {name}\n\n"
                 f"[Timer]\nOnCalendar={sched[1]}\nPersistent=true\n\n"
                 f"[Install]\nWantedBy=timers.target\n")
    d = out / "systemd"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.service").write_text(service, encoding="utf-8")
    (d / f"{name}.timer").write_text(timer, encoding="utf-8")
    return None


def _plist_calendar(task: dict) -> dict | None:
    trig = str(task.get("trigger", ""))
    m = TRIGGER_DAILY.fullmatch(trig)
    if m:
        return {"Hour": int(m.group(1)), "Minute": int(m.group(2))}
    m = TRIGGER_WEEKLY.fullmatch(trig)
    if m:
        dow = DOW.get(m.group(1).lower())
        if dow:
            return {"Weekday": dow[1], "Hour": int(m.group(2)),
                    "Minute": int(m.group(3))}
    m = TRIGGER_MONTHLY.fullmatch(trig)
    if m:
        return {"Day": int(m.group(1)), "Hour": int(m.group(2)),
                "Minute": int(m.group(3))}
    return None


def emit_launchd(task: dict, install_path: str, out: Path,
                 python: str | None = None) -> str | None:
    name = task["name"]
    argv = exec_argv(task, install_path, python)
    if argv is None:
        return f"skip {name}: kind={task.get('kind')} has no POSIX equivalent"
    label = f"{LAUNCHD_PREFIX}{name}"
    rep_raw = str(task.get("repeat_every", "") or "")
    rep = iso_seconds(rep_raw)
    trig = str(task.get("trigger", ""))
    # Built as a dict and serialized by plistlib: hand-written plist XML did not
    # escape values, so a path containing '&' or '<' produced invalid XML.
    plist: dict = {"Label": label, "ProgramArguments": argv}
    if trig == "AtStartup":
        plist["RunAtLoad"] = True
        # launchd has no boot-delay key, so an ignored startup_delay used to run
        # the task the instant the agent loaded — before the network/mounts the
        # delay exists to wait for. Express it as an explicit sleep instead.
        delay = iso_seconds(task.get("startup_delay", ""))
        if delay:
            plist["ProgramArguments"] = [
                "/bin/sh", "-c",
                f"sleep {delay}; exec " + " ".join(shlex.quote(a) for a in argv),
            ]
        # AtStartup + repeat_every. This used to fall through the `elif rep`
        # below and lose the repetition in SILENCE — the systemd side at least
        # returns a skip line. RunAtLoad covers the boot run and StartInterval
        # the repeat, which is exact here: an AtStartup task has no wall-clock
        # time to stay aligned to, so nothing is approximated.
        if rep:
            plist["StartInterval"] = rep
            if delay:
                print(f"  ! {name}: startup_delay={task['startup_delay']} is a sleep "
                      f"inside the command, so each StartInterval={rep}s repeat "
                      f"waits it out again")
        elif rep_raw:
            return (f"skip {name}: repeat_every={rep_raw} is not a duration "
                    f"launchd can express as StartInterval")
    elif rep:
        cal = _plist_calendar(task)
        # A registry "Daily 01:00 every PT4H" is an ALIGNED schedule. StartInterval
        # counts from whenever the agent was loaded, so the same declaration drifted
        # to arbitrary clock times on macOS. When the period divides the day evenly,
        # expand it into the explicit list of aligned times launchd does support.
        if cal is not None and "Hour" in cal and rep % 3600 == 0 and 24 % (rep // 3600) == 0:
            step = rep // 3600
            plist["StartCalendarInterval"] = [
                {**cal, "Hour": (cal["Hour"] + k) % 24}
                for k in range(0, 24, step)
            ]
        elif cal is None and trig not in TRIGGER_SIMPLE:
            return (f"skip {name}: repeat_every={task.get('repeat_every')} with "
                    f"trigger '{trig}' has no launchd equivalent")
        else:
            # No aligned expansion is possible (period does not divide 24h, or the
            # trigger carries no time of day) — fall back to an interval and SAY so,
            # instead of quietly pretending the alignment survived.
            plist["StartInterval"] = rep
            print(f"  ! {name}: launchd StartInterval={rep}s counts from load time — "
                  f"the aligned '{trig}' start time is not preserved")
    else:
        cal = _plist_calendar(task)
        if cal is None:
            return f"skip {name}: trigger '{trig}' unsupported for launchd"
        plist["StartCalendarInterval"] = cal
    if task.get("timeout_hours"):
        # No launchd equivalent of RuntimeMaxSec — say it rather than imply the
        # registry's timeout is in force.
        print(f"  ! {name}: timeout_hours={task['timeout_hours']} is not enforceable "
              f"under launchd (no RuntimeMaxSec equivalent)")
    d = out / "launchd"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{label}.plist", "wb") as f:
        plistlib.dump(plist, f)
    return None


def generate(tasks: list[dict], targets: list[str], install_path: str, out: Path,
             include_disabled: bool, verbose: bool = True, python: str | None = None) -> int:
    """Emit the units of every task that applies; return how many were written."""
    written = 0
    for task in tasks:
        if task.get("enabled") is False and not include_disabled:
            if verbose:
                print(f"  - {task['name']}: disabled in registry (use --all to include)")
            continue
        plat = str(task.get('platform', 'all')).lower()
        if plat not in ('all', 'posix'):
            if verbose:
                print(f"  - {task['name']}: platform={plat}, skipped (not POSIX)")
            continue
        for tgt in targets:
            note = (emit_systemd if tgt == "systemd" else emit_launchd)(task, install_path, out,
                                                                        python)
            if note:
                print(f"  ! {note}")
            else:
                written += 1
                if verbose:
                    print(f"  + {tgt}: {task['name']}")
    return written


def installed_units_dir(target: str) -> Path:
    """Where a per-user unit has to be for the init system to load it at all."""
    if target == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    # systemd --user reads $XDG_CONFIG_HOME/systemd/user, which falls back to
    # ~/.config when the variable is unset.
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def is_bundle_unit(target: str, filename: str, task_names: set[str]) -> bool:
    """Whether an installed file belongs to the bundle — the naming convention
    this generator emits, so somebody else's units are never called stale."""
    if target == "launchd":
        return filename.startswith(LAUNCHD_PREFIX) and filename.endswith(".plist")
    stem, _, suffix = filename.rpartition(".")
    # `Claude*` is the prefix every shipped task carries, and the one the enable
    # loop in INSTALL.md already treats as the bundle's; a registry name covers
    # a task of yours that does not follow it.
    return suffix in ("service", "timer") and (stem.startswith("Claude") or stem in task_names)


def check_units(generated: Path, installed: Path, target: str,
                task_names: set[str]) -> dict[str, list[str]]:
    """Compare freshly generated unit files with the installed ones, by content."""
    status: dict[str, list[str]] = {"new": [], "changed": [], "stale": [], "unchanged": []}
    fresh = {p.name: p for p in generated.iterdir() if p.is_file()} if generated.is_dir() else {}
    for name in sorted(fresh):
        have = installed / name
        if not have.is_file():
            status["new"].append(name)
        elif have.read_bytes() != fresh[name].read_bytes():
            status["changed"].append(name)
        else:
            status["unchanged"].append(name)
    if installed.is_dir():
        for p in sorted(installed.iterdir()):
            if p.is_file() and p.name not in fresh and is_bundle_unit(target, p.name, task_names):
                status["stale"].append(p.name)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["systemd", "launchd", "both"], default=None,
                    help="default: both; with --check, the init system of this OS")
    ap.add_argument("--install-path", default="~/.claude",
                    help="POSIX path the bundle is deployed to (replaces "
                         "<bundle-install-path> in registry script paths)")
    ap.add_argument("--out-dir", default="scheduler-units")
    ap.add_argument("--all", action="store_true",
                    help="include tasks marked enabled: false")
    ap.add_argument("--registry", default=None,
                    help="read this registry.yaml instead of the bundle source. "
                         "Use the DEPLOYED copy (<install-path>/cron/registry.yaml) "
                         "to regenerate units after editing your own schedule — "
                         "otherwise changing a trigger on POSIX meant editing the "
                         "repository, and the next `git pull` reverted it")
    ap.add_argument("--check", action="store_true",
                    help="write nothing; compare the units this registry generates "
                         "with the installed ones and exit 3 on any drift")
    ap.add_argument("--units-dir", default=None,
                    help="with --check: where the installed units are (default: "
                         "the per-user directory of the target)")
    ap.add_argument("--python", default=None,
                    help="absolute interpreter for python tasks (default: "
                         "`/usr/bin/env python3`, resolved on the init system's PATH)")
    args = ap.parse_args(argv)
    if args.target is None:
        # A Linux box has no LaunchAgents: checking both there would report every
        # plist as missing, which is noise, not drift.
        native = "launchd" if sys.platform == "darwin" else "systemd"
        args.target = native if args.check else "both"
    if args.units_dir and args.target == "both":
        ap.error("--units-dir names ONE directory — pass --target systemd or launchd")

    install_path = str(Path(args.install_path).expanduser()) \
        if args.install_path.startswith("~") else args.install_path
    out = Path(args.out_dir)

    registry = Path(args.registry).expanduser() if args.registry else REGISTRY
    if not registry.is_file():
        print(f"ERROR: registry not found: {registry}", file=sys.stderr)
        return 1
    print(f"registry: {registry}")
    tasks = load_tasks(registry)
    targets = ["systemd", "launchd"] if args.target == "both" else [args.target]

    if args.check:
        # "cp and hope" was the whole POSIX install: nothing compared what the
        # init system loads with what the registry now says, so an edited trigger
        # kept its old schedule and the timer of a removed task fired forever.
        names = {str(t.get("name")) for t in tasks if isinstance(t, dict)}
        drift = 0
        with tempfile.TemporaryDirectory() as tmp:
            generate(tasks, targets, install_path, Path(tmp), args.all, verbose=False,
                     python=args.python)
            for tgt in targets:
                units = Path(args.units_dir).expanduser() if args.units_dir \
                    else installed_units_dir(tgt)
                status = check_units(Path(tmp) / tgt, units, tgt, names)
                print(f"\n{tgt} units in {units}:")
                for kind in ("new", "changed", "stale"):
                    for name in status[kind]:
                        hint = ("  (no longer generated: disable it, then delete it)"
                                if kind == "stale" else "")
                        print(f"  {kind:<10} {name}{hint}")
                print(f"  {'unchanged':<10} {len(status['unchanged'])} file(s)")
                drift += len(status["new"]) + len(status["changed"]) + len(status["stale"])
        if drift:
            print(f"\nDRIFT: {drift} unit file(s) differ from the registry - "
                  f"reinstall the units (INSTALL.md, Linux / macOS notes)")
            return 3
        print("\nin sync: the installed units are exactly what the registry generates")
        return 0

    written = generate(tasks, targets, install_path, out, args.all, python=args.python)
    print(f"\nWrote {written} unit file(s) under {out}/")
    print("Enable them:")
    if "systemd" in targets:
        print(f"  cp {out}/systemd/*.{{service,timer}} ~/.config/systemd/user/ && "
              "systemctl --user daemon-reload && "
              "for t in ~/.config/systemd/user/Claude*.timer; do "
              "systemctl --user enable --now \"$(basename \"$t\")\"; done")
        # --user timers only fire while the user has an active login session,
        # UNLESS lingering is enabled — otherwise nightly/headless runs silently
        # never happen after logout/reboot (the POSIX analogue of Password-mode).
        print("  # To run these WITHOUT an active login (overnight / headless), "
              "enable lingering once:")
        print("  loginctl enable-linger \"$USER\"")
        print("  # (check with: loginctl show-user \"$USER\" -p Linger)")
    if "launchd" in targets:
        print(f"  cp {out}/launchd/*.plist ~/Library/LaunchAgents/ && "
              "for p in ~/Library/LaunchAgents/com.claude-bundle.*.plist; do "
              "launchctl load \"$p\"; done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
