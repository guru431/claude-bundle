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
import re
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
    if kind == "bash":
        return ["/bin/bash", script]
    if kind in ("python", "python_local"):
        return ["/usr/bin/env", "python3", script]
    return None  # cmd / vbs / exec — Windows-only, no POSIX equivalent


def systemd_oncalendar(task: dict) -> tuple[str, str] | None:
    """Return (kind, value) where kind is 'OnCalendar' or 'OnBootSec', or None."""
    trig = str(task.get("trigger", ""))
    rep_h = iso_hours(task.get("repeat_every", ""))
    m = re.fullmatch(r"Daily (\d{1,2}):(\d{2})", trig)
    if m:
        h, mi = int(m.group(1)), m.group(2)
        if rep_h:  # every rep_h hours starting at h (systemd step syntax)
            return ("OnCalendar", f"*-*-* {h:02d}/{rep_h}:{mi}:00")
        return ("OnCalendar", f"*-*-* {h:02d}:{mi}:00")
    m = re.fullmatch(r"Weekly (\w+) (\d{1,2}):(\d{2})", trig)
    if m:
        dow = DOW.get(m.group(1).lower())
        if dow:
            return ("OnCalendar", f"{dow[0]} *-*-* {int(m.group(2)):02d}:{m.group(3)}:00")
    m = re.fullmatch(r"Monthly day=(\d{1,2}) (\d{1,2}):(\d{2})", trig)
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
    exec_line = " ".join(argv)
    service = (
        f"[Unit]\nDescription={desc}\n\n"
        f"[Service]\nType=oneshot\nExecStart={exec_line}\n"
    )
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


def _plist_calendar(task: dict) -> str | None:
    trig = str(task.get("trigger", ""))
    m = re.fullmatch(r"Daily (\d{1,2}):(\d{2})", trig)
    if m:
        return (f"    <key>Hour</key><integer>{int(m.group(1))}</integer>\n"
                f"    <key>Minute</key><integer>{int(m.group(2))}</integer>")
    m = re.fullmatch(r"Weekly (\w+) (\d{1,2}):(\d{2})", trig)
    if m:
        dow = DOW.get(m.group(1).lower())
        if dow:
            return (f"    <key>Weekday</key><integer>{dow[1]}</integer>\n"
                    f"    <key>Hour</key><integer>{int(m.group(2))}</integer>\n"
                    f"    <key>Minute</key><integer>{int(m.group(3))}</integer>")
    m = re.fullmatch(r"Monthly day=(\d{1,2}) (\d{1,2}):(\d{2})", trig)
    if m:
        return (f"    <key>Day</key><integer>{int(m.group(1))}</integer>\n"
                f"    <key>Hour</key><integer>{int(m.group(2))}</integer>\n"
                f"    <key>Minute</key><integer>{int(m.group(3))}</integer>")
    return None


def emit_launchd(task: dict, install_path: str, out: Path) -> str | None:
    name = task["name"]
    argv = exec_argv(task, install_path)
    if argv is None:
        return f"skip {name}: kind={task.get('kind')} has no POSIX equivalent"
    label = f"com.claude-bundle.{name}"
    prog = "\n".join(f"    <string>{a}</string>" for a in argv)
    rep = iso_seconds(task.get("repeat_every", ""))
    trig = str(task.get("trigger", ""))
    if trig == "AtStartup":
        sched = "  <key>RunAtLoad</key>\n  <true/>"
    elif rep:  # a repeating task -> interval (aligned start dropped for launchd)
        sched = f"  <key>StartInterval</key>\n  <integer>{rep}</integer>"
    else:
        cal = _plist_calendar(task)
        if cal is None:
            return f"skip {name}: trigger '{trig}' unsupported for launchd"
        sched = f"  <key>StartCalendarInterval</key>\n  <dict>\n{cal}\n  </dict>"
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{label}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{prog}\n  </array>\n"
        f"{sched}\n</dict>\n</plist>\n"
    )
    d = out / "launchd"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{label}.plist").write_text(plist, encoding="utf-8")
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
    if "launchd" in targets:
        print(f"  cp {out}/launchd/*.plist ~/Library/LaunchAgents/ && "
              "for p in ~/Library/LaunchAgents/com.claude-bundle.*.plist; do "
              "launchctl load \"$p\"; done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
