# Windows shells, paths and file encodings

`~/.claude/CLAUDE.md` § Tool Selection Rules and § File Encoding carry the
rules. This is the detail: path forms per shell, sandbox quirks, and the byte
level of each script type.

## Path format — one style per shell

- **PowerShell / CMD** → backslashes with the drive letter: `C:\folder\sub`
- **Git Bash** → the POSIX mount form: `/c/folder/sub` (this is what `which`
  prints inside Git Bash, so it pastes back verbatim; CMD's `where` prints the
  `C:\...` form instead — don't paste that into Git Bash)
- Never mix the two in one command. Don't hand a `\backslash\` path to Git
  Bash, and don't hand a `/c/...` path to PowerShell.
- In Git Bash, resolve the root once into a variable: `D="/c/path/to/project"`
  then `"$D/file"`. Never `cd` — always absolute paths.
- A plain `bash` on Windows can be the WSL launcher (`System32\bash.exe`), which
  does not understand `C:/...` paths. For a project's `.sh` scripts call Git
  Bash explicitly: `& 'C:\Program Files\Git\bin\bash.exe' <script>`.

## Bash sandbox limitations (VS Code extension)

**Observed, not universal.** Reproduced on the Claude Code VS Code extension on
Windows (Git Bash), 2026-08. It is a property of one sandbox at one point in
time, not a fact about bash — if these commands work for you, they work.

- `echo`, `printf`, `ls`, `pwd`, `whoami`, `dir` may silently fail (exit 1/2).
  Where that happens it is normal — do not retry them.
- Does a file exist: `test -f "$path"`; a directory: `test -d "$path"`.
- List files with the Glob tool, not `ls`.

## Python on Windows

- Resolve the path once: `where python` or `python --version`.
- In Git Bash it is usually `"/c/Program Files/Python<ver>/python"` or just
  `python` — the `/c/...` form, per the path rule above.
- Use Python for data processing when shell pipes fail (they often do in the
  sandbox). "No module named pytest" in a bare system interpreter is an
  environment problem, not a red suite.

## File encodings — the byte level

- **PowerShell (.ps1).** Without a BOM, Windows PowerShell 5.1 reads the file in
  the system ANSI code page (whichever one your locale sets — CP1251 on a
  Russian install, CP1252 on a Western one, ...), never as UTF-8. Any non-ASCII
  byte is then mis-decoded — Cyrillic text, for example, turns into smart-quote
  characters that break string parsing. Add the BOM right after writing:
  ```bash
  python -c "
  f=r'path/to/file.ps1'
  b=open(f,'rb').read()
  if not b.startswith(bytes([0xEF,0xBB,0xBF])):
      open(f,'wb').write(bytes([0xEF,0xBB,0xBF])+b)
  "
  ```
  `Out-File` in PS 5.1 writes UTF-16 by default; `Set-Content` writes the ANSI
  code page — pass `-Encoding utf8` when another tool reads the file.
- **Bash (.sh).** A BOM breaks `#!/bin/bash`; a CR at the end of every line has
  bash looking for commands named `then\r`. UTF-8 without BOM, LF.
- **CMD/BAT (.cmd, .bat).** Non-ASCII text in the system ANSI code page (CP1251
  for Cyrillic, CP1252 for Western European, ...); UTF-8 only with
  `@chcp 65001` at the top. Line endings **CRLF, always**: with LF, cmd.exe
  misparses multi-line `( … )` blocks — `%1` expands once, `shift` stops moving
  the arguments, and a `goto` loop can spin at 100% CPU with no output and no
  child process. Another symptom of the same cause: `'X' is not recognized as
  an internal or external command` on fragments of lines. This applies to
  `.cmd` files that scripts or tests generate, too.
- **`.gitattributes`:** a glob like `dir/* text eol=lf` also catches the `.cmd`
  files in that directory, so `*.cmd text eol=crlf` must come LAST — the last
  matching rule wins.

The bundle's opt-in `text-encoding-guard.py` hook fixes the `.ps1` BOM and the
`.sh` BOM/CRLF after every write, and reports what it cannot fix safely.
