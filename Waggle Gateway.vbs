' Starts Ollama (local model) and the Hermes gateway (Telegram + Waggle's scheduled jobs) hidden at login.
' Copied into the Startup folder by install-startup.ps1.
Set sh = CreateObject("WScript.Shell")
local = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%")

Function OllamaUp()
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://localhost:11434/api/version", False
  http.Send
  OllamaUp = (Err.Number = 0 And http.Status = 200)
End Function

If Not OllamaUp() Then
  sh.Run """" & local & "\Programs\Ollama\ollama app.exe""", 0, False
  For i = 1 To 30
    WScript.Sleep 2000
    If OllamaUp() Then Exit For
  Next
End If

home = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.hermes"
sh.Environment("Process")("HERMES_HOME") = home
' A second copy exits on its own if the gateway is already running (it holds a lock).
sh.Run """" & home & "\bin\hermes.exe"" gateway run", 0, False
