' Starts ActivityWatch (aw-qt: the server and the window and AFK watchers) hidden at logon.
' Run by the scheduled task that tools\install-activitywatch-task.ps1 registers. Safe to run twice.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\ActivityWatch"
exe = dir & "\aw-qt.exe"
If Not fso.FileExists(exe) Then WScript.Quit

Function AwUp()
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://127.0.0.1:5600/api/0/info", False
  http.Send
  AwUp = (Err.Number = 0 And http.Status = 200)
End Function

If Not AwUp() Then
  sh.CurrentDirectory = dir
  sh.Run """" & exe & """", 0, False
End If
