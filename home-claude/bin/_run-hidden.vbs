' _run-hidden.vbs — hidden-window launcher for Windows Task Scheduler.
'
' Why: Task Scheduler launches console programs (bash, python, cmd) with a
' visible window unless wrapped. This wrapper calls them via WScript.Shell
' with window-style 0 (hidden).
'
' Usage (from registry sync-tasks.ps1):
'   wscript.exe _run-hidden.vbs <kind> <script> [arg1 arg2 ...]
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
Dim env, bashExe, pythonExe
Set env = shell.Environment("Process")
bashExe = env("BASH_EXE")
If bashExe = "" Then bashExe = "C:\Program Files\Git\bin\bash.exe"
pythonExe = env("PYTHON_EXE")
If pythonExe = "" Then pythonExe = "python.exe"

' Password-mode tasks fire in session 0, where the process env vars above may be
' empty (they aren't inherited from an interactive shell). Fall back to the
' bundle .env, which lives one level up from this script (<bundle>\.env), so an
' interpreter override survives before-logon. Only overrides values that are
' still at their hardcoded defaults; ignores every other key.
Dim fso, envPath, envFile, line, eqPos, envKey, envVal
On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
envPath = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName)) & "\.env"
If fso.FileExists(envPath) Then
    Set envFile = fso.OpenTextFile(envPath, 1)
    Do Until envFile.AtEndOfStream
        line = Trim(envFile.ReadLine)
        If line <> "" And Left(line, 1) <> "#" Then
            eqPos = InStr(line, "=")
            If eqPos > 0 Then
                envKey = Trim(Left(line, eqPos - 1))
                envVal = Trim(Mid(line, eqPos + 1))
                If Left(envVal, 1) = """" And Right(envVal, 1) = """" Then
                    envVal = Mid(envVal, 2, Len(envVal) - 2)
                End If
                If envKey = "BASH_EXE" And bashExe = "C:\Program Files\Git\bin\bash.exe" And envVal <> "" Then
                    bashExe = envVal
                ElseIf envKey = "PYTHON_EXE" And pythonExe = "python.exe" And envVal <> "" Then
                    pythonExe = envVal
                End If
            End If
        End If
    Loop
    envFile.Close
End If
On Error Goto 0

Select Case kind
    Case "bash"
        cmd = """" & bashExe & """ """ & script & """" & extra
    Case "python"
        cmd = """" & pythonExe & """ """ & script & """" & extra
    Case "cmd"
        cmd = "cmd /c """ & script & """" & extra
    Case Else
        WScript.Quit 3
End Select

' A LOG of launch failures. The launcher writes nothing anywhere, and Task
' Scheduler's Last Result is the only trace a task leaves — so when the launch
' itself failed there was no trace at all. One line into cron/logs/launcher.log
' is the difference between "the pipeline is broken somewhere" and "BASH_EXE
' points at a file that does not exist".
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
            " (set BASH_EXE / PYTHON_EXE in <bundle>\.env) — script: " & script
        WScript.Quit 9009
    End If
End If

' 0 = hidden window, True = wait for child to finish so the exit code propagates.
On Error Resume Next
rc = shell.Run(cmd, 0, True)
If Err.Number <> 0 Then
    LogLaunchFailure "FATAL: could not launch [" & cmd & "] — " & _
        Err.Number & " " & Err.Description
    Err.Clear
    On Error Goto 0
    WScript.Quit 9009
End If
On Error Goto 0
WScript.Quit rc
