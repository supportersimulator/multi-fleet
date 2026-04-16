"""Discord Fleet Bridge — P0 channel for fleet messaging via Discord bot.

Bidirectional relay: fleet daemon messages <-> Discord channel.
Each mac runs this bridge independently. Aaron can direct the fleet from phone.

Usage:
    FLEET_DISCORD_CHANNEL_ID=1234 python -m multifleet.discord_bridge
    # Or with env vars pre-set:
    python -m multifleet.discord_bridge
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.request
from collections import deque
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.discord-bridge")

# ── Graceful import of discord.py ──
try:
    import discord
    from discord.ext import tasks
    DISCORD_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    discord = None  # type: ignore[assignment]
    tasks = None  # type: ignore[assignment]
    DISCORD_AVAILABLE = False

# ── Constants ──
DAEMON_URL = "http://127.0.0.1:8855"
MIN_SEND_INTERVAL_S = 1.0
MAX_QUEUE = 500
POLL_INTERVAL_S = 5.0

# Node colors for embeds
_NODE_COLORS = {
    "mac1": 0x3498DB,   # blue (chief)
    "mac2": 0x2ECC71,   # green
    "mac3": 0xF1C40F,   # yellow
}
_TYPE_COLORS = {
    "alert": 0xFF0000, "urgent": 0xFF4500, "critique": 0xFF8C00,
    "context": 0x3498DB, "heartbeat": 0x95A5A6, "ack": 0x95A5A6,
}
_DEFAULT_COLOR = 0x7289DA


# ── Validation ──

def validate_channel_id(channel_id: Any) -> bool:
    if channel_id is None:
        return False
    s = str(channel_id).strip()
    return s.isdigit() and len(s) >= 17


def validate_bot_token(token: Any) -> bool:
    if not token or not isinstance(token, str):
        return False
    return len(token.strip()) > 0


# ── Token retrieval ──

def get_bot_token() -> Optional[str]:
    """Retrieve bot token: env var first, then macOS Keychain."""
    env = os.environ.get("DISCORD_BOT_TOKEN")
    if env:
        return env.strip()
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "fleet", "-s", "DISCORD_BOT_TOKEN", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# ── Message formatting ──

def format_fleet_embed(msg: dict) -> dict:
    """Convert fleet message dict to Discord embed dict."""
    msg_type = msg.get("type", "context")
    source = msg.get("from", "unknown")
    payload = msg.get("payload") or {}
    subject = payload.get("subject", "")
    body = payload.get("body", "")

    color = _NODE_COLORS.get(source, _TYPE_COLORS.get(msg_type, _DEFAULT_COLOR))
    title = subject or f"[{msg_type.upper()}] from {source}"

    fields = []
    if body:
        # Truncate to Discord's 1024 char limit per field
        fields.append({"name": "Details", "value": str(body)[:1024], "inline": False})
    if msg.get("to"):
        fields.append({"name": "To", "value": str(msg["to"]), "inline": True})

    return {
        "title": title[:256],
        "description": f"**{source}** | {msg_type}",
        "color": color,
        "fields": fields,
    }


def _write_seed_file(node_id: str, content: str, author_name: str, is_command: bool = False):
    """Write Discord message as seed file for Claude Code fleet-inbox-hook injection.

    Every Discord message in the fleet channel becomes a seed file at
    /tmp/fleet-seed-discord-<timestamp>.md. The fleet-inbox-hook.sh picks
    these up on next user prompt and injects into Claude Code context.
    """
    import time as _time
    ts = int(_time.time())
    seed_path = f"/tmp/fleet-seed-discord-{ts}.md"
    msg_type = "DISCORD CMD" if is_command else "DISCORD"
    try:
        with open(seed_path, "w") as f:
            f.write(f"## [{msg_type}] from {author_name} (via Discord)\n\n")
            f.write(f"{content}\n")
        # Touch trigger file — LaunchAgent watches this to wake Claude Code
        trigger = "/tmp/discord-wake-trigger"
        with open(trigger, "w") as f:
            f.write(str(ts))
        logger.info("Seed file written: %s (trigger touched)", seed_path)
    except Exception as e:
        logger.warning("Failed to write seed file: %s", e)


def parse_discord_message(content: str, author_id: str = "unknown") -> Optional[dict]:
    """Parse Discord message into fleet payload. Returns None if not a fleet command.

    Commands:
        !fleet send <target> <message>
        !fleet broadcast <message>
        !fleet status
    """
    if not content.startswith("!fleet "):
        return None
    parts = content[7:].split(None, 2)
    if not parts:
        return None

    cmd = parts[0]
    if cmd == "status":
        return {"type": "status_request", "from": f"discord:{author_id}"}
    elif cmd == "broadcast" and len(parts) >= 2:
        return {
            "type": "context", "from": f"discord:{author_id}", "to": "all",
            "payload": {"subject": "Discord broadcast", "body": " ".join(parts[1:])},
        }
    elif cmd == "send" and len(parts) >= 3:
        return {
            "type": "context", "from": f"discord:{author_id}", "to": parts[1],
            "payload": {"subject": f"Discord -> {parts[1]}", "body": parts[2]},
        }
    return None


# ── Bridge ──

class DiscordFleetBridge:
    """Bidirectional Discord <-> Fleet daemon relay."""

    RATE_LIMIT_WINDOW_S = MIN_SEND_INTERVAL_S

    def __init__(self, bot_token: str, channel_id: str):
        self.bot_token = bot_token
        self.channel_id = str(channel_id)
        self.node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._connected = False
        self._queue: deque[dict] = deque(maxlen=MAX_QUEUE)
        self._last_send_time: float = 0.0
        self._seen_ids: set = set()
        self._client: Any = None
        self._channel: Any = None

    @property
    def pending_queue_size(self) -> int:
        return len(self._queue)

    def queue_message(self, msg: dict) -> None:
        now = time.monotonic()
        if (now - self._last_send_time) >= self.RATE_LIMIT_WINDOW_S and self._connected:
            self._last_send_time = now
        else:
            self._queue.append(msg)

    def send_to_discord(self, msg: dict) -> dict:
        if not self._connected:
            self._queue.append(msg)
            return {"sent": False, "queued": True}
        now = time.monotonic()
        if (now - self._last_send_time) < self.RATE_LIMIT_WINDOW_S:
            self._queue.append(msg)
            return {"sent": False, "queued": True}
        self._last_send_time = now
        return {"sent": True, "queued": False}

    def drain_queue(self) -> int:
        count = 0
        while self._queue:
            self._queue.popleft()
            count += 1
            self._last_send_time = time.monotonic()
        return count

    def run(self):
        """Start the Discord bot (blocking)."""
        if not DISCORD_AVAILABLE:
            logger.error("discord.py not installed. Run: pip install discord.py")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            self._channel = client.get_channel(int(self.channel_id))
            if not self._channel:
                logger.error("Channel %s not found. Check FLEET_DISCORD_CHANNEL_ID.", self.channel_id)
                await client.close()
                return
            self._connected = True
            logger.info("Discord bridge connected: %s in #%s", client.user, self._channel.name)
            # Send hello
            embed = discord.Embed(
                title=f"[{self.node_id}] Fleet Bridge Online",
                description=f"Node **{self.node_id}** connected to fleet daemon.",
                color=_NODE_COLORS.get(self.node_id, _DEFAULT_COLOR),
            )
            await self._channel.send(embed=embed)
            # Start polling daemon
            if not poll_daemon.is_running():
                poll_daemon.start()

        @client.event
        async def on_message(message):
            if message.author == client.user:
                return
            if message.channel.id != int(self.channel_id):
                return

            content = message.content.strip()
            author_name = str(message.author.display_name or message.author.name)
            parsed = parse_discord_message(content, str(message.author.id))

            # Write seed file for Claude Code injection on ANY message
            # (not just !fleet commands — Aaron directing from iPad should inject)
            _write_seed_file(self.node_id, content, author_name, is_command=parsed is not None)

            if not parsed:
                # Not a !fleet command but still injected via seed file
                await message.add_reaction("\U0001F4E8")  # envelope
                return
            # Forward to fleet daemon
            try:
                data = json.dumps(parsed).encode()
                req = urllib.request.Request(
                    f"{DAEMON_URL}/message", data=data,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                urllib.request.urlopen(req, timeout=3)
                await message.add_reaction("\u2705")  # checkmark
            except Exception as e:
                logger.warning("Failed to forward to daemon: %s", e)
                await message.add_reaction("\u274C")  # red X

        @tasks.loop(seconds=POLL_INTERVAL_S)
        async def poll_daemon():
            """Poll fleet daemon for messages to relay to Discord."""
            if not self._channel:
                return
            try:
                req = urllib.request.Request(f"{DAEMON_URL}/messages/unread")
                resp = urllib.request.urlopen(req, timeout=3)
                messages = json.loads(resp.read())
                if not isinstance(messages, list):
                    return
                for msg in messages:
                    msg_id = msg.get("id", "")
                    if msg_id in self._seen_ids:
                        continue
                    self._seen_ids.add(msg_id)
                    # Prune seen set
                    if len(self._seen_ids) > 500:
                        self._seen_ids = set(list(self._seen_ids)[-250:])
                    # Don't echo messages that came from Discord
                    if str(msg.get("from", "")).startswith("discord:"):
                        continue
                    embed_data = format_fleet_embed(msg)
                    embed = discord.Embed(
                        title=embed_data["title"],
                        description=embed_data["description"],
                        color=embed_data["color"],
                    )
                    for f in embed_data.get("fields", []):
                        embed.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
                    embed.set_footer(text=f"Fleet Node: {msg.get('from', '?')}")
                    await self._channel.send(embed=embed)
            except Exception:
                pass  # Daemon may be unavailable — non-blocking

        logger.info("Starting Discord bridge for node %s...", self.node_id)
        client.run(self.bot_token, log_handler=None)


# ── Module-level helpers ──

def status() -> dict:
    token = get_bot_token()
    channel_id = os.environ.get("FLEET_DISCORD_CHANNEL_ID", "")
    return {
        "configured": validate_bot_token(token) and validate_channel_id(channel_id),
        "connected": False,
        "discord_py_installed": DISCORD_AVAILABLE,
        "channel_id": channel_id if validate_channel_id(channel_id) else None,
        "token_present": validate_bot_token(token),
    }


def main():
    """Entry point for python -m multifleet.discord_bridge."""
    if not DISCORD_AVAILABLE:
        print("ERROR: discord.py not installed. Run: pip install discord.py")
        return

    token = get_bot_token()
    if not validate_bot_token(token):
        print("ERROR: No bot token. Set DISCORD_BOT_TOKEN env var or store in Keychain:")
        print("  security add-generic-password -a fleet -s DISCORD_BOT_TOKEN -w 'YOUR_TOKEN'")
        return

    channel_id = os.environ.get("FLEET_DISCORD_CHANNEL_ID", "")
    if not validate_channel_id(channel_id):
        print("ERROR: Set FLEET_DISCORD_CHANNEL_ID env var to your Discord channel ID")
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bridge = DiscordFleetBridge(bot_token=token, channel_id=channel_id)
    bridge.run()


if __name__ == "__main__":
    main()
