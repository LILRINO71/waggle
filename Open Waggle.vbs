' Desktop / Start-menu / Ctrl+Alt+W: show the Waggle voice window, or start it if it isn't running.
Set sh = CreateObject("WScript.Shell")
If Not sh.AppActivate("Waggle Voice") Then
  here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
  sh.Run """" & here & "\Waggle Voice.bat""", 1, False
End If
