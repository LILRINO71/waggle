' Starts the Hermes gateway (Telegram + Waggle's scheduled jobs) hidden at login.
' Copied into the Startup folder by install-startup.ps1.
Set sh = CreateObject("WScript.Shell")
home = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.hermes"
sh.Environment("Process")("HERMES_HOME") = home
sh.Run """" & home & "\bin\hermes.exe"" gateway run", 0, False
