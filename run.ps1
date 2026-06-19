# ===================================================================
#  Voice OS (Windows) launcher (PowerShell).
#  Creates a venv, installs deps, loads .env, runs in HOLD-TO-TALK.
#  Usage:   .\run.ps1            (hold F13 to talk; bind it to a mouse button)
#           .\run.ps1 --push-to-talk
#  If PowerShell blocks the script, run this once:
#       Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# ===================================================================
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- venv ---
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv 2>$null
    if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
}
& ".venv\Scripts\Activate.ps1"

# --- deps (only once) ---
if (-not (Test-Path ".venv\.installed")) {
    Write-Host "Installing dependencies..."
    python -m pip install --upgrade pip | Out-Null
    python -m pip install -r requirements-windows.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "Dependency install failed."; exit 1 }
    "done" | Out-File ".venv\.installed"
}

# --- run loop: exit code 42 means "reboot" (voice command) -> relaunch.
#     .env is reloaded each launch so a reboot also picks up edits to it.
do {
    if (Test-Path ".env") {
        Get-Content ".env" | ForEach-Object {
            $line = $_.Trim()
            if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
                $k, $v = $line.Split("=", 2)
                [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim())
            }
        }
    }

    if (-not $env:OPENAI_API_KEY) {
        Write-Host "OPENAI_API_KEY is not set. Put it in .env first."
        exit 1
    }

    # --- run (default = hold-to-talk on F13) ---
    python voice_agent.py @args
    $code = $LASTEXITCODE
    if ($code -eq 42) { Write-Host "`n=========  Rebooting Voice OS  =========" }
} while ($code -eq 42)
