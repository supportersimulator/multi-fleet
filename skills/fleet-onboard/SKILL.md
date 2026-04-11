---
name: fleet-onboard
description: Run the fleet onboarding wizard — checks what's configured, what's missing, and guides new users through setup
---

# Fleet Onboard

Interactive onboarding wizard for new fleet nodes. Runs 10 diagnostic checks and generates a step-by-step setup guide.

## Usage

```bash
# Render the setup guide (markdown)
python3 -m multifleet.onboarding

# JSON output for automation
python3 -m multifleet.onboarding --json

# Override node ID
python3 -m multifleet.onboarding --node-id mac4
```

### Via MCP tool

Use the `fleet_onboard` MCP tool to get onboarding status and guide in one call.

### Via Python

```python
from multifleet.onboarding import OnboardingWizard

wiz = OnboardingWizard()
status = wiz.check_all()
print(f"Ready: {status['ready']} — {status['progress_pct']:.0f}%")
print(wiz.render_guide())
```

## Checks

| # | Check | Required | What it verifies |
|---|-------|----------|-----------------|
| 1 | python | Yes | python3 in PATH and working |
| 2 | nats | Yes | NATS server reachable on port 4222 |
| 3 | daemon | Yes | Fleet daemon responding on port 8855 |
| 4 | keychain | Yes | HMAC key stored in macOS Keychain |
| 5 | superset | No | Superset API key configured |
| 6 | config | Yes | .multifleet/config.json exists and parses |
| 7 | ssh | No | SSH keys loaded in agent |
| 8 | probes | No | Probe modules present |
| 9 | surgeons | No | 3-surgeons MCP plugin installed |
| 10 | skills | No | Skills directory populated |

## When to use

- Setting up a brand new machine in the fleet
- After a fresh OS install or environment reset
- Debugging "why can't this node communicate?"
- Verifying a node is fully operational before assigning work
