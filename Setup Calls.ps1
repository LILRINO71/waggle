# One-time setup for Waggle phone calls (free Twilio trial). You type the values here; they are saved
# only to ~/.waggle/twilio.env on this PC and never shown or committed. Re-run any time: values that are
# already saved and valid are kept, so you only fill in what's missing.
$envFile = "$env:USERPROFILE\.waggle\twilio.env"
New-Item -ItemType Directory -Force (Split-Path $envFile) | Out-Null

$saved = @{}
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding utf8) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*?)\s*$') { $saved[$Matches[1]] = $Matches[2] }
    }
}

function Format-Phone($raw) {
    $d = ($raw -replace '[^\d+]', '')
    if ($d -match '^\d{10}$') { $d = "+1$d" }          # US number typed without the country code
    elseif ($d -match '^1\d{10}$') { $d = "+$d" }
    return $d
}

$rules = [ordered]@{
    TWILIO_ACCOUNT_SID = @{ ask = "Account SID (starts with AC, from the Twilio Console home page)"; ok = '^AC[0-9a-fA-F]{32}$'; secret = $false }
    TWILIO_AUTH_TOKEN  = @{ ask = "Auth Token (hidden as you type)"; ok = '^[0-9a-fA-F]{32}$'; secret = $true }
    TWILIO_FROM        = @{ ask = "Your TWILIO phone number (Console > Phone Numbers), e.g. +1 951 555 0100"; ok = '^\+\d{10,15}$'; secret = $false; phone = $true }
    TWILIO_TO          = @{ ask = "YOUR own cell number (the one you verified with Twilio), e.g. 951 555 0199"; ok = '^\+\d{10,15}$'; secret = $false; phone = $true }
}

$missing = @($rules.Keys | Where-Object { -not ($saved[$_] -match $rules[$_].ok) })
if ($missing.Count -eq 0) {
    Write-Host "Everything is already saved."
} else {
    Write-Host "Waggle phone calls - setup"
    if ($missing -contains 'TWILIO_ACCOUNT_SID') {
        Write-Host "1. Sign up free at https://www.twilio.com/try-twilio (verify your own phone number when it asks)."
        Write-Host "2. In the Twilio Console, click 'Get a phone number' (free on the trial)."
        Start-Process "https://www.twilio.com/try-twilio"
    }
    foreach ($k in $rules.Keys) {
        $r = $rules[$k]
        if ($saved[$k] -match $r.ok) { Write-Host "  $k : already saved"; continue }
        while ($true) {
            if ($r.secret) {
                $sec = Read-Host $r.ask -AsSecureString
                $v = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
            } else { $v = Read-Host $r.ask }
            $v = $v.Trim()
            if ($r.phone) { $v = Format-Phone $v }
            if ($v -match $r.ok) { $saved[$k] = $v; break }
            Write-Host "  That doesn't look right, try again." -ForegroundColor Yellow
        }
    }
    $body = ($rules.Keys | ForEach-Object { "$_=$($saved[$_])" }) -join "`n"
    [IO.File]::WriteAllText($envFile, "$body`n")
    Write-Host "Saved to $envFile."
}

Write-Host "Placing a test call now..."
Push-Location $PSScriptRoot
python -m waggle.call "Calls are working. I will only ring for things that matter."
Pop-Location
Write-Host "If your phone rang: done. On the trial a short Twilio notice plays first; press any key to hear Waggle."
Read-Host "Press Enter to close"
