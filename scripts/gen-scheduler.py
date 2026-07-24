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

Then follow the printed enable instructions (systemctl --user enable --now, or
launchctl load).
"""
from __future__ import annotations

import argparse
import plistlib
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "home-claude" / "cron" / "registry.yaml"

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


def load_tasks() -> list[dict]:
    text = REGISTRY.read_text(encoding="utf-8")
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


def exec_argv(task: dict, install_path: str) -> list[str] | None:
    kind = task.get("kind", "bash")
    script = posix_script(task, install_path)
    extra = [str(a) for a in (task.get("script_args") or [])]
    if kind == "bash":
        return ["/bin/bash", script] + extra
    if kind in ("python", "python_local"):
        return ["/usr/bin/env", "python3", script] + extra
    return None  # cmd / vbs / exec — Windows-only, no POSIX equivalent


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart token. systemd splits the command line with
    shell-like quoting rules, and expands '%' as a specifier prefix even
    inside quotes — so '%' must be doubled regardless."""
    return shlex.quote(arg).replace("%", "%%")


def systemd_oncalendar(task: dict) -> tuple[str, str] | None:
    """Return (kind, value) where kind is 'OnCalendar' or 'OnBootSec', or None."""
    trig = str(task.get("trigger", ""))
    rep_h = iso_hours(task.get("repeat_every", ""))
    m = TRIGGER_DAILY.fullmatch(trig)
    if m:
        h, mi = int(m.group(1)), m.group(2)
        if rep_h:  # every rep_h hours starting at h (systemd step syntax)
            return ("OnCalendar", f"*-*-* {h:02d}/{rep_h}:{mi}:00")
        return ("OnCalendar", f"*-*-* {h:02d}:{mi}:00")
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


def emit_systemd(task: dict, install_path: str, out: Path) -> str | None:
    name = task["name"]
    argv = exec_argv(task, install_path)
    if argv is None:
        return f"skip {name}: kind={task.get('kind')} has no POSIX equivalent"
    sched = systemd_oncalendar(task)
    if sched is None:
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


def emit_launchd(task: dict, install_path: str, out: Path) -> str | None:
    name = task["name"]
    argv = exec_argv(task, install_path)
    if argv is None:
        return f"skip {name}: kind={task.get('kind')} has no POSIX equivalent"
    label = f"com.claude-bundle.{name}"
    rep = iso_seconds(task.get("repeat_every", ""))
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=["systemd", "launchd", "both"], default="both")
    ap.add_argument("--install-path", default="~/.claude",
                    help="POSIX path the bundle is deployed to (replaces "
                         "<bundle-install-path> in registry script paths)")
    ap.add_argument("--out-dir", default="scheduler-units")
    ap.add_argument("--all", action="store_true",
                    help="include tasks marked enabled: false")
    args = ap.parse_args()

    install_path = str(Path(args.install_path).expanduser()) \
        if args.install_path.startswith("~") else args.install_path
    out = Path(args.out_dir)

    tasks = load_tasks()
    targets = ["systemd", "launchd"] if args.target == "both" else [args.target]
    written = 0
    for task in tasks:
        if task.get("enabled") is False and not args.all:
            print(f"  - {task['name']}: disabled in registry (use --all to include)")
            continue
        plat = str(task.get('platform', 'all')).lower()
        if plat not in ('all', 'posix'):
            print(f"  - {task['name']}: platform={plat}, skipped (not POSIX)")
            continue
        for tgt in targets:
            note = (emit_systemd if tgt == "systemd" else emit_launchd)(task, install_path, out)
            if note:
                print(f"  ! {note}")
            else:
                written += 1
                print(f"  + {tgt}: {task['name']}")

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
