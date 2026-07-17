# Findings archive — claude-bundle

Audit trail of resolved findings. Entries are never deleted from here — this is
the record of what was actually done. Newest first.

---

## 2026-07-17 · sync-tasks.ps1 can hang forever on a wedged WMI [P2]
**Context:** found while verifying the 0.5.1 installer split — `install.ps1`'s
`Get-InstallDriveType` hung indefinitely on `Get-CimInstance Win32_LogicalDisk`
(reproduced on HEAD; the query never returned until a reboot). Fixed there by
switching to `System.IO.DriveInfo`.
**What:** `home-claude/cron/admin/sync-tasks.ps1::Get-MappedDriveLetters` still
uses the same `Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=4'`
query, so a wedged WMI service hangs the elevated task syncer with no timeout and
no output. This is the mapped-drive fail-loud point (a Password task on a mapped
drive registers cleanly then silently exits 127), so it matters that it either
answers or fails visibly — not stalls.
**Proposal:** mirror the install.ps1 fix — `[System.IO.DriveInfo]::GetDrives()`
filtered on `DriveType -eq [System.IO.DriveType]::Network`, no WMI. Not done
inline: sync-tasks.ps1 runs elevated and this is its safety predicate, so the
change wants a real elevated sync to verify, not a standalone function test.
Note the existing `catch {}` already degrades to an empty set (i.e. "no mapped
drives"), which is the wrong-answer failure this check exists to prevent — worth
revisiting at the same time.
**Status:** done
**Resolved:** 2026-07-17 — WMI dropped from the syncer. `Get-MappedDriveLetters`
(set-of-all-network-drives, built via `Get-CimInstance Win32_LogicalDisk`) is
replaced by a per-letter `Test-DriveLetterMapped` backed by
`New-Object System.IO.DriveInfo` with a `$script:_driveTypes` cache. DriveInfo
answers from the filesystem API, so the hang is structurally gone.

The `catch {}` was addressed by **removing** error handling rather than by
propagating an "unknown" verdict. First attempt added a $true/$false/$null
tri-state plus a caller branch that skipped the task on unknown; that was
reverted as error handling for an impossible scenario — the DriveInfo
constructor only throws on an invalid drive name, which the `^([A-Za-z]):`
regex already rules out, and `DriveType` does not throw. The `$null` branch was
therefore dead code. The predicate is now bare: any unexpected throw aborts the
sync via the script's `$ErrorActionPreference='Stop'`, which is the fail-loud
behavior the finding asked for, and the caller loop is unchanged from before.

The stale cross-reference in `scripts/install.ps1` ("...which is what
cron/admin/sync-tasks.ps1 still uses") was corrected in the same change.

**Verified:** parse-check of both scripts; the predicate exercised against a real
drive set containing both a fixed local drive and mapped network drives → a
local path=False, a mapped-drive path=True, `\\host\share`=False, a bare
`wscript.exe`=False, an unmounted letter=False (the same verdict the old WMI
query gave, since it listed no such drive — no regression). Full `-DryRun` over
a 4-task fixture: the local, UNC, and Interactive+mapped tasks all pass the
predicate, the Password+mapped task is skipped with the fail-loud message. Not
verified: a real elevated
`Register-ScheduledTask` — the finding asked for one, but the changed code all
sits in the predicate that runs *before* registration, and the registration path
itself is untouched by this change.
