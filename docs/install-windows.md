# Install on Windows

**Status: experimental.** Native Windows is supported for single-node use and CLI testing, but several channels rely on POSIX primitives (osascript, launchd, ssh config files, UDP multicast heartbeats). **WSL2 is strongly recommended** for full functionality — in WSL2, follow [install-linux.md](install-linux.md) instead.

This guide covers native Windows 10/11 install. Launchd-backed features (P8 direct text input on macOS, macOS notifications) are not available.

## Prerequisites

- Windows 10 21H2+ or Windows 11
- Python 3.10+ from [python.org](https://www.python.org/downloads/windows/) (check "Add to PATH")
- `nats-server.exe` from [GitHub releases](https://github.com/nats-io/nats-server/releases/latest) (`nats-server-vX.Y.Z-windows-amd64.zip`)
- PowerShell 7+ recommended (`winget install Microsoft.PowerShell`)
- Optional: [nssm](https://nssm.cc/download) for Windows Service wrapping

### Recommended: WSL2

```powershell
wsl --install -d Ubuntu-22.04
```

Then inside Ubuntu: follow [install-linux.md](install-linux.md). All channels work.

## Install NATS server (native)

```powershell
# Download and extract the Windows zip from GitHub releases.
# Move nats-server.exe to a permanent location, e.g.:
New-Item -ItemType Directory -Force -Path "C:\Program Files\nats-server"
Move-Item nats-server.exe "C:\Program Files\nats-server\"
# Add to PATH (user scope):
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Program Files\nats-server", "User")
nats-server.exe --version
```

## Install Multi-Fleet

```powershell
pip install multifleet
pip install 'multifleet[full]'   # optional cryptography extras

fleet --version
fleet doctor
```

## Secret storage (Windows Credential Manager)

Use the cross-platform `keyring` package — it auto-selects Windows Credential Manager on Windows:

```powershell
pip install keyring
python -c "import keyring; keyring.set_password('multifleet', 'hmac', 'YOUR_KEY')"
# Multi-Fleet reads via: keyring.get_password('multifleet', 'hmac')
```

Alternatively, an environment variable (less secure — visible in process env):

```powershell
[Environment]::SetEnvironmentVariable("MULTIFLEET_HMAC_KEY", "YOUR_KEY", "User")
```

## Run as a Windows Service

### Option A: `nssm` (easiest)

```powershell
# Install nssm, then:
nssm install MultifleetNats "C:\Program Files\nats-server\nats-server.exe" "-p 4222 -m 8222"
nssm start MultifleetNats

nssm install MultifleetNerve "C:\Python312\python.exe" "-m multifleet.nats_client"
nssm set MultifleetNerve AppEnvironmentExtra `
  MULTIFLEET_NODE_ID=pc1 `
  FLEET_NERVE_PORT=8855
nssm start MultifleetNerve
```

### Option B: `sc.exe` (built-in)

```powershell
sc.exe create MultifleetNats binPath= "\"C:\Program Files\nats-server\nats-server.exe\" -p 4222" start= auto
sc.exe start MultifleetNats
```

For the Python daemon with `sc.exe` you typically need a wrapper script. `nssm` is simpler.

### Option C: Run manually in a terminal

```powershell
$env:MULTIFLEET_NODE_ID = "pc1"
fleet-nerve
```

## Verify

```powershell
Invoke-RestMethod http://127.0.0.1:8855/health
fleet status
fleet doctor
```

## Known gotchas

| Gotcha | Fix |
|--------|-----|
| Line endings break seed files | Force LF: `git config --global core.autocrlf input` |
| Path separator errors in config | Use forward slashes in `config.json`, or escape backslashes: `C:\\Users\\...` |
| ANSI colors garbled in CMD | Use Windows Terminal or PowerShell 7+; or `$env:NO_COLOR=1` |
| `fleet: command not found` | Check Python Scripts dir is on PATH: `python -m site --user-site` |
| SSH channels (P4/P5) fail | Install OpenSSH client: `Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0` |
| Wake-on-LAN (P6) unreliable | Windows power settings disable WoL by default; enable in NIC properties |
| launchd channel (P8) unavailable | macOS-only. Windows falls through to P7 Git push |

## Feature support matrix

| Channel | Native Windows | WSL2 |
|---------|---------------|------|
| P0 Cloud | OK | OK |
| P1 NATS | OK | OK |
| P2 HTTP | OK | OK |
| P3 Chief relay | OK | OK |
| P4 Seed file (SSH) | Needs OpenSSH | OK |
| P5 SSH direct | Needs OpenSSH | OK |
| P6 Wake-on-LAN | Limited | OK |
| P7 Git push | OK | OK |
| P8 Direct text input | Not supported (macOS osascript) | Not supported |

When in doubt: **use WSL2**.
