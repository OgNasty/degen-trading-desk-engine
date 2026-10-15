' Runs a .cmd with no console window and waits for it (pcmove 2026-10-10).
Dim a, i, cmd
Set a = WScript.Arguments
cmd = "cmd.exe /c """ & """" & a(0) & """"
For i = 1 To a.Count - 1
  cmd = cmd & " """ & a(i) & """"
Next
cmd = cmd & """"
WScript.Quit CreateObject("WScript.Shell").Run(cmd, 0, True)
