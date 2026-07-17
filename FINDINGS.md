# Findings — claude-bundle
Побочные находки. Ревизия: MonthlyStratReview 1-го числа. Stale >90 дней → alert.

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
**Status:** open
