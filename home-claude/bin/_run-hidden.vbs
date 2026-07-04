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
'   0   — child exited 0
'   N   — child exit code
'   2   — bad arguments
'   3   — unknown kind

Option Explicit

If WScript.Arguments.Count < 2 Then
    WScript.Quit 2
End If

Dim kind, script, i, extra, cmd, shell, rc
kind   = LCase(WScript.Arguments(0))
script = WScript.Arguments(1)

extra = ""
For i = 2 To WScript.Arguments.Count - 1
    extra = extra & " """ & WScript.Arguments(i) & """"
Next

Set shell = CreateObject("WScript.Shell")

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

' 0 = hidden window, True = wait for child to finish so the exit code propagates.
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
