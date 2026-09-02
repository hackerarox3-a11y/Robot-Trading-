param(
    [ValidateSet("SIMULATION", "PAPER", "LIVE")]
    [string]$Mode = "SIMULATION",
    [ValidateSet("mt5", "deriv", "both")]
    [string]$Broker = "mt5"
)

Set-Location $PSScriptRoot
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env cree. Remplis les variables avant un lancement LIVE."
    if ($Mode -eq "LIVE") {
        notepad ".env"
        exit 1
    }
}

py main.py --mode $Mode --broker $Broker