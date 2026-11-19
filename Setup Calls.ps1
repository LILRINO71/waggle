# One-time setup for Waggle phone calls (free Twilio trial). You type the values here; they are saved
# only to ~/.waggle/twilio.env on this PC and never shown or committed.
$envFile = "$env:USERPROFILE\.waggle\twilio.env"
New-Item -ItemType Directory -Force (Split-Path $envFile) | Out-Null

Write-Host @"
Waggle phone calls - setup
1. Sign up free at https://www.twilio.com/try-twilio (verify your own phone number when it asks).
2. In the Twilio Console, click 'Get a phone number' (free on the trial).
3. On the Console home page, copy the Account SID and Auth Token.
"@
Start-Process "https://www.twilio.com/try-twilio"
Read-Host "Press Enter when you have them"

$sid = Read-Host "Account SID (starts with AC)"
$tokSecure = Read-Host "Auth Token (hidden)" -AsSecureString
$tok = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($tokSecure))
$from = Read-Host "Your Twilio phone number, like +15551234567"
$to = Read-Host "YOUR phone number (the verified one), like +15557654321"

$body = "TWILIO_ACCOUNT_SID=$($sid.Trim())`nTWILIO_AUTH_TOKEN=$($tok.Trim())`nTWILIO_FROM=$($from.Trim())`nTWILIO_TO=$($to.Trim())`n"
[IO.File]::WriteAllText($envFile, $body)
Write-Host "Saved to $envFile. Placing a test call now..."
Push-Location $PSScriptRoot
python -m waggle.call "Calls are working. I will only ring for things that matter."
Pop-Location
Write-Host "If your phone rang: done. On the trial a short Twilio notice plays first; press any key to hear Waggle."
Read-Host "Press Enter to close"
