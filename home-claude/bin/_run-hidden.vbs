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

Select Case kind
    Case "bash"
        cmd = "bash """ & script & """" & extra
    Case "python"
        cmd = "python """ & script & """" & extra
    Case "cmd"
        cmd = "cmd /c """ & script & """" & extra
    Case Else
        WScript.Quit 3
End Select

Set shell = CreateObject("WScript.Shell")
' 0 = hidden window, True = wait for child to finish so the exit code propagates.
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
