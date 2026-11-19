' Starts Ollama (local model) and the Hermes gateway (Telegram + Waggle's scheduled jobs) hidden at login.
' Copied into the Startup folder by install-startup.ps1.
Set sh = CreateObject("WScript.Shell")
local = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%")

Function OllamaUp()
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://127.0.0.1:11434/api/version", False
  http.Send
  OllamaUp = (Err.Number = 0 And http.Status = 200)
End Function

' Flash attention + 8-bit KV cache: the 64k-token window then fits the GPU (otherwise 30% runs on the CPU).
sh.Environment("Process")("OLLAMA_FLASH_ATTENTION") = "1"
sh.Environment("Process")("OLLAMA_KV_CACHE_TYPE") = "q8_0"
If Not OllamaUp() Then
  sh.Run """" & local & "\Programs\Ollama\ollama app.exe""", 0, False
  For i = 1 To 30
    WScript.Sleep 2000
    If OllamaUp() Then Exit For
  Next
End If

Function KokoroUp()
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://127.0.0.1:8899/", False
  http.Send
  KokoroUp = (Err.Number = 0)
End Function

' Keep the Kokoro voice model loaded so spoken replies skip the 1.3 s model load.
profile = sh.ExpandEnvironmentStrings("%USERPROFILE%")
kpy = profile & "\.waggle\kokoro\venv\Scripts\pythonw.exe"
ksay = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\voice\kokoro_say.py"
If Not CreateObject("Scripting.FileSystemObject").FileExists(ksay) Then ksay = profile & "\Claude\waggle\voice\kokoro_say.py"
If Not KokoroUp() Then sh.Run """" & kpy & """ """ & ksay & """ --serve", 0, False

home = profile & "\.hermes"
sh.Environment("Process")("HERMES_HOME") = home
' Hermes's own ffmpeg: Telegram voice notes (speech in and out) need it.
sh.Environment("Process")("PATH") = home & "\tools\ffmpeg-9.0.1-win32-x64\bin;" & sh.ExpandEnvironmentStrings("%PATH%")
' A second copy exits on its own if the gateway is already running (it holds a lock).
sh.Run """" & home & "\bin\hermes.exe"" gateway run", 0, False
