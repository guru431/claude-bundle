' _run-hidden.vbs — hidden-window launcher for Windows Task Scheduler.
'
' Why: Task Scheduler launches console programs (bash, python, cmd) with a
' visible window unless wrapped. This wrapper calls them via WScript.Shell
' with window-style 0 (hidden).
'
' Usage (from registry sync-tasks.ps1):
'   wscript.exe //B //nologo _run-hidden.vbs <kind> <script> [arg1 arg2 ...]
'   //B   = batch mode: no banner and NO MODAL DIALOG on an error. A task fires
'           in session 0, where a dialog nobody can dismiss holds the task open
'           until its execution time limit. Host options are consumed by wscript
'           itself, so WScript.Arguments still starts at <kind>.
'   <kind>   = bash | python | cmd
'   <script> = absolute path (UNC or local C:\) — never a mapped drive for
'              Password-mode tasks
'
' Exit codes:
'   0    — child exited 0
'   N    — child exit code
'   2    — bad arguments
'   3    — unknown kind
'   9009 — the interpreter could not be launched (ERROR_FILE_NOT_FOUND, the
'          same code cmd.exe uses). This one is the important addition: a
'          missing or mistyped BASH_EXE / PYTHON_EXE made shell.Run raise a
'          runtime error, WScript.Quit was never reached, the host exited 0,
'          and Task Scheduler recorded Last Result 0. The task monitor then had
'          nothing to alert about and the night was silently empty.

Option Explicit

If WScript.Arguments.Count < 2 Then
    WScript.Quit 2
End If

Dim kind, script, i, extra, cmd, shell, rc
kind   = LCase(WScript.Arguments(0))
script = WScript.Arguments(1)

' Each extra argument is re-quoted, and an embedded quote is DOUBLED first.
' Without that an argument that already carries `"` — which is what
' sync-tasks.ps1 produced from a registry `script_args` entry — closed the
' quoting early, and everything after it was re-split by the child's own parser.
' It happened to work for `"--full"` because the two layers of quoting cancelled
' out; an argument containing a space would have fallen apart.
extra = ""
For i = 2 To WScript.Arguments.Count - 1
    extra = extra & " """ & Replace(WScript.Arguments(i), """", """""") & """"
Next

Set shell = CreateObject("WScript.Shell")

' Python encodes stdout in the console code page, so any task redirecting
' non-ASCII output to a file ('>> log 2>&1') produced mojibake — the log became
' unreadable exactly when someone opened it to debug. A PROCESS-scope variable
' is inherited by every child (bash -> python, cmd -> python), so this one line
' covers all bash/python/cmd tasks instead of a per-task 'set PYTHONIOENCODING='.
shell.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"

' Resolve the interpreter path — do NOT invoke bash/python by bare name. A
' Password-mode task fires in session 0 with only the SYSTEM PATH, and a default
' Git-for-Windows install puts just Git\cmd there (git.exe), NOT Git\bin where
' bash.exe lives — so a bare "bash" can raise file-not-found and abort the task
' with no log. Use a sane default and allow an override via the BASH_EXE /
' PYTHON_EXE process env vars (the same vars the peer cron scripts honor).
Dim env, bashExe, pythonExe, bashIsDefault, pythonIsDefault, bashCand
Set env = shell.Environment("Process")
bashExe = env("BASH_EXE")
bashIsDefault = (bashExe = "")
If bashIsDefault Then bashExe = "C:\Program Files\Git\bin\bash.exe"
pythonExe = env("PYTHON_EXE")
pythonIsDefault = (pythonExe = "")
If pythonIsDefault Then pythonExe = "python.exe"

' Password-mode tasks fire in session 0, where the process env vars above may be
' empty (they aren't inherited from an interactive shell). Fall back to the
' bundle .env, which lives one level up from this script (<bundle>\.env), so an
' interpreter override survives before-logon. Only overrides values that are
' still at their hardcoded defaults; ignores every other key.
Dim fso, envPath, dotEnv
On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
envPath = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)) & "\.env"
On Error Goto 0
Set dotEnv = ReadDotEnv(envPath)
' Exists() before the lookup: reading a missing key ADDS it to a Dictionary.
If bashIsDefault And dotEnv.Exists("BASH_EXE") Then
    If dotEnv("BASH_EXE") <> "" Then
        bashExe = dotEnv("BASH_EXE")
        bashIsDefault = False
    End If
End If
If pythonIsDefault And dotEnv.Exists("PYTHON_EXE") Then
    If dotEnv("PYTHON_EXE") <> "" Then
        pythonExe = dotEnv("PYTHON_EXE")
        pythonIsDefault = False
    End If
End If

' ---- .env parser: begin (tests/test_dotenv_parity.py runs this block) ----
' The VBScript member of the bundle's four .env parsers. What every one of them
' must read out of a file is pinned by tests/fixtures/dotenv-parity.env. Keep
' the block self-contained and ASCII-only: the test lifts it into a harness.
'
' ADODB.Stream, not FileSystemObject.OpenTextFile. OpenTextFile reads in the
' system ANSI codepage, so a UTF-8 BOM became part of the first key (and that
' variable silently went missing), and a non-ASCII interpreter path arrived as
' mojibake, failed FileExists below and ended every task with 9009. The utf-8
' charset consumes the BOM itself. Now read as the other parsers read them:
' `export KEY=`, one pair of single or double quotes, tabs, CRLF or LF.
Function ReadDotEnv(path)
    Dim result, stream, text, lines, n, line, eqPos, key, value, identifier
    Set result = CreateObject("Scripting.Dictionary")
    Set ReadDotEnv = result
    On Error Resume Next
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 2                    ' adTypeText
    stream.Charset = "utf-8"
    stream.Open
    stream.LoadFromFile path
    text = stream.ReadText(-1)         ' adReadAll
    stream.Close
    If Err.Number <> 0 Then            ' no .env, or unreadable: nothing to add
        Err.Clear
        Exit Function
    End If
    On Error Goto 0
    Set identifier = New RegExp
    identifier.Pattern = "^[A-Za-z_][A-Za-z0-9_]*$"
    lines = Split(Replace(Replace(text, vbCrLf, vbLf), vbCr, vbLf), vbLf)
    For n = 0 To UBound(lines)
        line = TrimBlank(lines(n))
        If Left(line, 7) = "export " Then line = TrimBlank(Mid(line, 8))
        eqPos = InStr(line, "=")
        If Left(line, 1) <> "#" And eqPos > 1 Then
            key = TrimBlank(Left(line, eqPos - 1))
            value = TrimBlank(Mid(line, eqPos + 1))
            If Len(value) >= 2 Then
                If (Left(value, 1) = """" And Right(value, 1) = """") Or _
                   (Left(value, 1) = "'" And Right(value, 1) = "'") Then
                    value = Mid(value, 2, Len(value) - 2)
                End If
            End If
            ' The first occurrence wins, as it does in the Python and bash parsers.
            If identifier.Test(key) Then
                If Not result.Exists(key) Then result.Add key, value
            End If
        End If
    Next
End Function

' Trim() removes spaces only; every other parser trims tabs as well.
Function TrimBlank(s)
    Dim blank, first, last
    blank = " " & vbTab & vbCr & vbLf & Chr(11) & Chr(12)
    first = 1
    last = Len(s)
    Do While first <= last
        If InStr(blank, Mid(s, first, 1)) = 0 Then Exit Do
        first = first + 1
    Loop
    Do While last >= first
        If InStr(blank, Mid(s, last, 1)) = 0 Then Exit Do
        last = last - 1
    Loop
    TrimBlank = Mid(s, first, last - first + 1)
End Function
' ---- .env parser: end ----

' Still on the hardcoded default: probe BOTH bash.exe locations a
' Git-for-Windows install can have. Git\bin\bash.exe is the usual one, but the
' MinGW/MSYS layout puts the real binary at Git\usr\bin\bash.exe and some
' installs (and portable unpacks) ship only that. utils.py::find_bash,
' cron/lib/runtime.sh and the tests all try both; this launcher tried one, so a
' machine with the other layout failed every bash task with 9009 while every
' other part of the bundle found bash fine. BASH_EXE (env or .env) still wins.
If bashIsDefault Then
    For Each bashCand In Array("C:\Program Files\Git\bin\bash.exe", _
                               "C:\Program Files\Git\usr\bin\bash.exe")
        If fso.FileExists(bashCand) Then
            bashExe = bashCand
            Exit For
        End If
    Next
End If

Select Case kind
    Case "bash"
        cmd = """" & bashExe & """ """ & script & """" & extra
    Case "python"
        cmd = """" & pythonExe & """ """ & script & """" & extra
    Case "cmd"
        ' /s plus ONE more pair of quotes around the whole command. Without /s,
        ' cmd.exe keeps the quotes only when the line holds exactly two of them;
        ' with a quoted argument as well it strips the first and the last quote
        ' of the line, so `cmd /c "C:\p q\x.cmd" "arg"` ran `C:\p` - exit 1,
        ' nothing executed (reproduced with cscript).
        cmd = "cmd /s /c """"" & script & """" & extra & """"
    Case Else
        WScript.Quit 3
End Select

' A LOG of launch failures. The launcher writes nothing anywhere, and Task
' Scheduler's Last Result is the only trace a task leaves — so when the launch
' itself failed there was no trace at all. One line into cron/logs/launcher.log
' is the difference between "the pipeline is broken somewhere" and "BASH_EXE
' points at a file that does not exist".
' Keep the MESSAGES passed in here ASCII-only: OpenTextFile writes in the system
' ANSI codepage, so an em dash lands as mojibake in the one line whose whole job
' is to be readable at 03:00. The comments in this file may use them; the log
' text may not.
Sub LogLaunchFailure(message)
    Dim logDir, logPath, logFile
    On Error Resume Next
    logDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)) & "\cron\logs"
    If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
    logPath = logDir & "\launcher.log"
    Set logFile = fso.OpenTextFile(logPath, 8, True)
    logFile.WriteLine Now & " " & message
    logFile.Close
    On Error Goto 0
End Sub

' Check the interpreter EXISTS before trying to run it: the error message can
' then name the file, which a WSH runtime error cannot.
Dim exePath
exePath = ""
If kind = "bash" Then exePath = bashExe
If kind = "python" Then exePath = pythonExe
If exePath <> "" And InStr(exePath, "\") > 0 Then
    If Not fso.FileExists(exePath) Then
        LogLaunchFailure "FATAL: " & kind & " interpreter not found: " & exePath & _
            " (set BASH_EXE / PYTHON_EXE in <bundle>\.env) - script: " & script
        WScript.Quit 9009
    End If
End If

' 0 = hidden window, True = wait for child to finish so the exit code propagates.
On Error Resume Next
rc = shell.Run(cmd, 0, True)
If Err.Number <> 0 Then
    LogLaunchFailure "FATAL: could not launch [" & cmd & "] - " & _
        Err.Number & " " & Err.Description
    Err.Clear
    On Error Goto 0
    WScript.Quit 9009
End If
On Error Goto 0
WScript.Quit rc
