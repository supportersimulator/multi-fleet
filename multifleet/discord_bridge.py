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
from pathlib import Path
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
THREE_SURGEONS_URL = "http://127.0.0.1:3541"
MIN_SEND_INTERVAL_S = 1.0
MAX_QUEUE = 500
POLL_INTERVAL_S = 5.0
IMAGE_DIR = Path("/tmp/fleet-discord-images")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}

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


async def _download_attachments(attachments: list) -> list[str]:
    """Download image attachments from Discord message. Returns list of saved file paths."""
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    loop = asyncio.get_event_loop()
    for att in attachments:
        ext = Path(att.filename).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        ts = int(time.time() * 1000)
        dest = IMAGE_DIR / f"{ts}_{att.filename}"
        try:
            req = urllib.request.Request(att.url)
            resp = await loop.run_in_executor(None, lambda r=req: urllib.request.urlopen(r, timeout=30))
            data = await loop.run_in_executor(None, resp.read)
            dest.write_bytes(data)
            saved.append(str(dest))
            logger.info("Saved attachment: %s (%d bytes)", dest, len(data))
        except Exception as e:
            logger.warning("Failed to download attachment %s: %s", att.filename, e)
    return saved


def _write_seed_file(node_id: str, content: str, author_name: str, is_command: bool = False,
                     image_paths: list[str] | None = None):
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
            if image_paths:
                f.write(f"\n### Attached Images ({len(image_paths)})\n")
                for img in image_paths:
                    f.write(f"- **Image**: `{img}`\n")
                f.write("\nATLAS: Use the Read tool on each image path above to view them.\n")
        # Touch trigger file — LaunchAgent watches this to wake Claude Code
        trigger = "/tmp/discord-wake-trigger"
        with open(trigger, "w") as f:
            f.write(str(ts))
        logger.info("Seed file written: %s (trigger touched, images: %d)", seed_path, len(image_paths or []))
    except Exception as e:
        logger.warning("Failed to write seed file: %s", e)


def parse_3s_command(content: str) -> Optional[dict]:
    """Parse !3s commands. Returns dict with 'cmd' and optional 'topic', or None.

    Commands:
        !3s probe
        !3s consult <topic>
        !3s cross-exam <topic>
    """
    if not content.startswith("!3s "):
        return None
    rest = content[4:].strip()
    if not rest:
        return None
    parts = rest.split(None, 1)
    cmd = parts[0]
    topic = parts[1] if len(parts) > 1 else None

    if cmd == "probe":
        return {"cmd": "probe"}
    elif cmd == "consult" and topic:
        return {"cmd": "consult", "topic": topic}
    elif cmd == "cross-exam" and topic:
        return {"cmd": "cross_examine", "topic": topic}
    return None


async def call_3s_api(parsed: dict) -> dict:
    """Call 3-surgeons HTTP API. Returns {"ok": bool, "data": ..., "error": ...}."""
    cmd = parsed["cmd"]
    loop = asyncio.get_event_loop()
    try:
        if cmd == "probe":
            req = urllib.request.Request(f"{THREE_SURGEONS_URL}/tool/probe")
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
        else:
            # consult or cross_examine
            body = json.dumps({"topic": parsed["topic"]}).encode()
            req = urllib.request.Request(
                f"{THREE_SURGEONS_URL}/tool/{cmd}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=30))
        data = json.loads(resp.read())
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def parse_discord_message(content: str, author_id: str = "unknown") -> Optional[dict]:
    """Parse Discord message into fleet payload. Returns None if not a fleet command.

    Commands:
        !fleet send <target> <message>
        !fleet broadcast <message>
        !fleet status
        !fleet search <query>           — search Discord channel history
        !fleet report <node> <status>   — report node progress
        !fleet progress                 — show all node progress
    """
    if not content.startswith("!fleet "):
        return None
    parts = content[7:].split(None, 2)
    if not parts:
        return None

    cmd = parts[0]
    if cmd == "status":
        return {"type": "status_request", "from": f"discord:{author_id}"}
    elif cmd == "search" and len(parts) >= 2:
        query = " ".join(parts[1:])
        return {"type": "history_search", "from": f"discord:{author_id}", "query": query}
    elif cmd == "report" and len(parts) >= 3:
        return {
            "type": "progress_report", "from": f"discord:{author_id}",
            "node": parts[1], "status_text": parts[2],
        }
    elif cmd == "progress":
        return {"type": "progress_query", "from": f"discord:{author_id}"}
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


# ── Progress Ledger (per-node return-and-report) ──

_PROGRESS_DB = Path("/tmp/fleet-progress-ledger.json")


def _load_progress() -> dict:
    """Load progress ledger from disk. Returns {node_id: [entries]}."""
    if _PROGRESS_DB.exists():
        try:
            return json.loads(_PROGRESS_DB.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_progress(data: dict) -> None:
    """Persist progress ledger."""
    try:
        _PROGRESS_DB.write_text(json.dumps(data, indent=2))
    except OSError as e:
        logger.warning("Failed to save progress ledger: %s", e)


def record_progress(node_id: str, status_text: str, source: str = "discord") -> dict:
    """Record a progress entry for a node. Returns the entry."""
    data = _load_progress()
    entries = data.setdefault(node_id, [])
    entry = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": status_text,
        "source": source,
    }
    entries.append(entry)
    # Keep last 50 entries per node
    if len(entries) > 50:
        data[node_id] = entries[-50:]
    _save_progress(data)
    return entry


def get_progress_summary() -> str:
    """Get formatted progress summary for all nodes."""
    data = _load_progress()
    if not data:
        return "No progress reports recorded yet."
    lines = []
    for node in sorted(data.keys()):
        entries = data[node]
        if not entries:
            continue
        latest = entries[-1]
        age_s = time.time() - latest["ts"]
        age_str = f"{int(age_s)}s" if age_s < 60 else f"{int(age_s/60)}m" if age_s < 3600 else f"{int(age_s/3600)}h"
        lines.append(f"**{node}** ({age_str} ago): {latest['status']}")
        if len(entries) > 1:
            lines.append(f"  ↳ {len(entries)} total reports")
    return "\n".join(lines) if lines else "No progress reports recorded."


def get_node_progress(node_id: str, limit: int = 10) -> str:
    """Get detailed progress for a specific node."""
    data = _load_progress()
    entries = data.get(node_id, [])
    if not entries:
        return f"No progress reports for {node_id}."
    lines = [f"**{node_id}** — {len(entries)} report(s):"]
    for entry in entries[-limit:]:
        lines.append(f"  [{entry['iso']}] {entry['status']}")
    return "\n".join(lines)


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

            # ── Download image attachments ──
            image_paths: list[str] = []
            if message.attachments:
                image_paths = await _download_attachments(list(message.attachments))

            # ── !3s commands → 3-surgeons HTTP API (bypass fleet daemon) ──
            parsed_3s = parse_3s_command(content)
            if parsed_3s:
                _write_seed_file(self.node_id, content, author_name, is_command=True, image_paths=image_paths)
                await message.add_reaction("\U0001F52C")  # microscope — thinking
                result = await call_3s_api(parsed_3s)
                if result["ok"]:
                    data = result["data"]
                    # Build response text from whatever the API returned
                    if isinstance(data, dict):
                        resp_text = data.get("result") or data.get("summary") or json.dumps(data, indent=2)
                    else:
                        resp_text = str(data)
                    embed = discord.Embed(
                        title=f"3-Surgeons: {parsed_3s['cmd']}",
                        description=str(resp_text)[:4096],
                        color=0xE74C3C,  # red — surgery
                    )
                    if parsed_3s.get("topic"):
                        embed.set_footer(text=f"Topic: {parsed_3s['topic']}")
                    await message.channel.send(embed=embed)
                    await message.add_reaction("\u2705")  # checkmark
                else:
                    embed = discord.Embed(
                        title="3-Surgeons: Error",
                        description=f"Failed to reach 3-surgeons API:\n```{result['error'][:1000]}```",
                        color=0xFF0000,
                    )
                    await message.channel.send(embed=embed)
                    await message.add_reaction("\u274C")  # red X
                return

            parsed = parse_discord_message(content, str(message.author.id))

            # Write seed file for Claude Code injection on ANY message
            # (not just !fleet commands — Aaron directing from iPad should inject)
            _write_seed_file(self.node_id, content, author_name, is_command=parsed is not None,
                             image_paths=image_paths)

            if not parsed:
                # Not a !fleet command but still injected via seed file
                if image_paths:
                    await message.add_reaction("\U0001F4F7")  # camera — image saved
                else:
                    await message.add_reaction("\U0001F4E8")  # envelope
                return

            # ── Handle history search locally (Discord API, not fleet daemon) ──
            if parsed["type"] == "history_search":
                await message.add_reaction("\U0001F50D")  # magnifying glass
                query = parsed["query"].lower()
                matches = []
                try:
                    async for hist_msg in self._channel.history(limit=500):
                        if hist_msg.author == client.user and hist_msg.embeds:
                            # Search embed content (fleet messages)
                            for emb in hist_msg.embeds:
                                searchable = f"{emb.title or ''} {emb.description or ''}"
                                for f_obj in (emb.fields or []):
                                    searchable += f" {f_obj.value}"
                                if query in searchable.lower():
                                    matches.append({
                                        "time": hist_msg.created_at.strftime("%m/%d %H:%M"),
                                        "title": (emb.title or "")[:80],
                                        "snippet": (emb.description or "")[:120],
                                    })
                        elif query in (hist_msg.content or "").lower():
                            matches.append({
                                "time": hist_msg.created_at.strftime("%m/%d %H:%M"),
                                "title": f"@{hist_msg.author.display_name}",
                                "snippet": hist_msg.content[:120],
                            })
                        if len(matches) >= 15:
                            break
                except Exception as e:
                    logger.warning("History search failed: %s", e)
                    await message.channel.send(f"Search error: {e}")
                    await message.add_reaction("\u274C")
                    return

                if not matches:
                    await message.channel.send(f"No results for: `{parsed['query']}`")
                else:
                    result_lines = [f"**{m['time']}** {m['title']}: {m['snippet']}" for m in matches]
                    embed = discord.Embed(
                        title=f"Search: \"{parsed['query']}\" ({len(matches)} results)",
                        description="\n".join(result_lines)[:4096],
                        color=0x3498DB,
                    )
                    await message.channel.send(embed=embed)
                await message.add_reaction("\u2705")
                return

            # ── Handle progress report (local ledger) ──
            if parsed["type"] == "progress_report":
                entry = record_progress(parsed["node"], parsed["status_text"], source=str(message.author))
                embed = discord.Embed(
                    title=f"Progress: {parsed['node']}",
                    description=f"Recorded: {parsed['status_text']}",
                    color=_NODE_COLORS.get(parsed["node"], _DEFAULT_COLOR),
                )
                embed.set_footer(text=f"Logged at {entry['iso']}")
                await message.channel.send(embed=embed)
                await message.add_reaction("\u2705")
                return

            # ── Handle progress query ──
            if parsed["type"] == "progress_query":
                summary = get_progress_summary()
                embed = discord.Embed(
                    title="Fleet Progress — All Nodes",
                    description=summary[:4096],
                    color=0x9B59B6,
                )
                await message.channel.send(embed=embed)
                await message.add_reaction("\u2705")
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

def report_to_discord(node_id: str, status_text: str, channel_id: str = "") -> dict:
    """Programmatic return-and-report: push progress from any node to Discord.

    Usable from CLI, daemon hooks, or any Python caller.
    Records to local ledger AND posts to Discord via fleet daemon relay.

    Delivery priority: fleet daemon (relay to bridge) → Discord REST API direct.

    Returns {"ok": bool, "ledger": entry_dict, "discord": bool}.
    """
    # Record locally first (always succeeds)
    entry = record_progress(node_id, status_text, source="auto")

    discord_sent = False

    # Try 1: Route through fleet daemon → bridge bot (preferred, bot has permissions)
    try:
        msg = {
            "type": "progress_report",
            "from": node_id,
            "to": "discord",
            "payload": {
                "subject": f"[{node_id.upper()}] Progress Report",
                "body": status_text,
            },
        }
        payload = json.dumps(msg).encode()
        req = urllib.request.Request(
            f"{DAEMON_URL}/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        discord_sent = True
    except Exception as e:
        logger.debug("Daemon relay failed, trying REST: %s", e)

    # Try 2: Direct Discord REST API (fallback if daemon not running)
    if not discord_sent:
        token = get_bot_token()
        ch_id = channel_id or os.environ.get("FLEET_DISCORD_CHANNEL_ID", "")
        if validate_bot_token(token) and validate_channel_id(ch_id):
            try:
                color = _NODE_COLORS.get(node_id, _DEFAULT_COLOR)
                embed_data = {
                    "title": f"[{node_id.upper()}] Progress Report",
                    "description": status_text[:4096],
                    "color": color,
                    "footer": {"text": f"Auto-report at {entry['iso']}"},
                }
                rest_payload = json.dumps({"embeds": [embed_data]}).encode()
                req = urllib.request.Request(
                    f"https://discord.com/api/v10/channels/{ch_id}/messages",
                    data=rest_payload,
                    headers={
                        "Authorization": f"Bot {token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
                discord_sent = True
            except Exception as e:
                logger.warning("report_to_discord REST fallback failed: %s", e)

    return {"ok": True, "ledger": entry, "discord": discord_sent}


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
