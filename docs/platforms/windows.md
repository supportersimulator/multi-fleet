# Windows Setup

> **Status: Experimental / Community Support.** Multi-Fleet is developed and tested on macOS and Linux. Windows works but is not the primary target. Contributions welcome.

---

## Prerequisites

- Python 3.10+ ([python.org](https://www.python.org/downloads/windows/))
- Git for Windows ([git-scm.com](https://git-scm.com/download/win))
- NATS server binary ([nats.io releases](https://github.com/nats-io/nats-server/releases))

---

## Install NATS Server

Download the Windows zip from [NATS releases](https://github.com/nats-io/nats-server/releases), extract `nats-server.exe`, and place it on your PATH (e.g., `C:\nats\`).

```powershell
# Verify
nats-server.exe --version

# Manual start
nats-server.exe -p 4222 -m 8222
```

### NATS as a Windows Service

```powershell
# Install as service using sc.exe
sc.exe create "NATS-Server" binpath= "C:\nats\nats-server.exe -p 4222 -m 8222" start= auto
sc.exe start NATS-Server

# Check status
sc.exe query NATS-Server

# Remove service
sc.exe stop NATS-Server
sc.exe delete NATS-Server
```

---

## Fleet Daemon Auto-Start

### Option A: Task Scheduler (Recommended)

```powershell
# Create a scheduled task that starts at logon
$action = New-ScheduledTaskAction `
  -Execute "python" `
  -Argument "$env:USERPROFILE\dev\er-simulator-superrepo\tools\fleet_nerve_nats.py serve" `
  -WorkingDirectory "$env:USERPROFILE\dev\er-simulator-superrepo"

$trigger = New-ScheduledTaskTrigger -AtLogon

$settings = New-ScheduledTaskSettingsSet `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 365)

Register-ScheduledTask `
  -TaskName "Fleet-Nerve" `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Multi-Fleet Nerve Daemon"
```

### Option B: Windows Service (via NSSM)

[NSSM](https://nssm.cc/) wraps any executable as a Windows service:

```powershell
nssm install FleetNerve python "$env:USERPROFILE\dev\er-simulator-superrepo\tools\fleet_nerve_nats.py" serve
nssm set FleetNerve AppDirectory "$env:USERPROFILE\dev\er-simulator-superrepo"
nssm set FleetNerve AppEnvironmentExtra "MULTIFLEET_NODE_ID=%COMPUTERNAME%" "NATS_URL=nats://127.0.0.1:4222"
nssm start FleetNerve
```

### Manage

```powershell
# Task Scheduler
Get-ScheduledTask -TaskName "Fleet-Nerve"
Start-ScheduledTask -TaskName "Fleet-Nerve"
Stop-ScheduledTask -TaskName "Fleet-Nerve"

# NSSM service
nssm status FleetNerve
nssm restart FleetNerve
```

---

## Secret Management (Credential Manager)

Use Windows Credential Manager via PowerShell:

```powershell
# Store a secret
cmdkey /generic:fleet-nerve-ANTHROPIC_API_KEY /user:fleet-nerve /pass:"sk-ant-..."

# Read a secret (from Python)
# pip install keyring
python -c "import keyring; print(keyring.get_password('fleet-nerve', 'ANTHROPIC_API_KEY'))"

# List stored credentials
cmdkey /list:fleet-nerve*

# Delete a credential
cmdkey /delete:fleet-nerve-ANTHROPIC_API_KEY
```

Alternatively, use an environment file with restricted ACL:

```powershell
# Create env file
Set-Content "$env:USERPROFILE\.fleet-nerve\env" @"
MULTIFLEET_NODE_ID=$env:COMPUTERNAME
NATS_URL=nats://127.0.0.1:4222
ANTHROPIC_API_KEY=sk-ant-...
"@

# Restrict to current user only
icacls "$env:USERPROFILE\.fleet-nerve\env" /inheritance:r /grant:r "${env:USERNAME}:(R)"
```

---

## Windows Firewall

```powershell
# Allow NATS (port 4222)
New-NetFirewallRule `
  -DisplayName "NATS Server" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 4222 `
  -Action Allow `
  -Profile Private

# Allow Fleet Nerve (port 8855)
New-NetFirewallRule `
  -DisplayName "Fleet Nerve Daemon" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8855 `
  -Action Allow `
  -Profile Private

# LAN-only (restrict to subnet)
New-NetFirewallRule `
  -DisplayName "NATS Server (LAN)" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 4222 `
  -RemoteAddress 192.168.1.0/24 `
  -Action Allow

# List fleet rules
Get-NetFirewallRule | Where-Object { $_.DisplayName -match "NATS|Fleet" }

# Remove a rule
Remove-NetFirewallRule -DisplayName "NATS Server"
```

---

## Verify

```powershell
Invoke-RestMethod http://127.0.0.1:8855/health
Invoke-RestMethod http://127.0.0.1:8222/varz
fleet status
fleet doctor
```

---

## Known Limitations

- xbar menu bar plugins are macOS-only. Use `fleet dashboard --web` instead.
- `caffeinate` is macOS-only. Use `powercfg -change -standby-timeout-ac 0` to prevent sleep.
- SSH-based channels (P4, P5) require OpenSSH for Windows or WSL.
- Wake-on-LAN (P6) requires a wired Ethernet connection and BIOS/UEFI configuration.
