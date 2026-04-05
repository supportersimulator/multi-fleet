"""
Fleet message sanitization — defense against prompt injection, credential leaks, and oversized payloads.

Fleet messages flow from untrusted sources (other nodes, seed files, git inbox) into Claude
sessions via systemMessage output and seed file injection. Without sanitization, a compromised
node could inject LLM role markers or prompt override instructions into another node's session.

This module provides three layers of defense:
1. strip_llm_markers()     — Remove LLM role/instruction markers that could hijack model behavior
2. strip_prompt_injections() — Remove known prompt injection patterns
3. limit_message_size()    — Enforce configurable size limits to prevent context flooding
4. sanitize_message()      — Apply all three in sequence (the standard entry point)
5. SanitizingLogFilter     — logging.Filter that redacts API keys from log output
"""

import logging
import os
import re
from typing import Optional


# -- Size limits --

# Default max message size in bytes. Configurable via FLEET_MAX_MESSAGE_BYTES env var.
DEFAULT_MAX_MESSAGE_BYTES = 10 * 1024  # 10KB


# -- LLM role markers --
# These strings, if present in fleet messages, could cause the LLM to interpret
# injected content as system/user/assistant turns rather than data. We strip them
# to prevent role confusion attacks.

_LLM_MARKER_PATTERNS = [
    # OpenAI / ChatML format
    re.compile(r"<\|system\|>", re.IGNORECASE),
    re.compile(r"<\|user\|>", re.IGNORECASE),
    re.compile(r"<\|assistant\|>", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"<\|endoftext\|>", re.IGNORECASE),
    # Llama / Mistral instruction format
    re.compile(r"\[INST\]", re.IGNORECASE),
    re.compile(r"\[/INST\]", re.IGNORECASE),
    re.compile(r"<<SYS>>", re.IGNORECASE),
    re.compile(r"<</SYS>>", re.IGNORECASE),
    # Anthropic format
    re.compile(r"\bHuman:\s*$", re.MULTILINE),
    re.compile(r"\bAssistant:\s*$", re.MULTILINE),
    # Generic XML-style role tags that could confuse models
    re.compile(r"<\|(?:tool|function|observation)\|>", re.IGNORECASE),
]


# -- Prompt injection patterns --
# Known phrases used in prompt injection attacks. We match conservatively to avoid
# false positives on legitimate discussion of these topics — patterns require word
# boundaries and are case-insensitive.

_PROMPT_INJECTION_PATTERNS = [
    # Direct override attempts
    re.compile(r"\bIGNORE\s+ALL\s+PREVIOUS\s+INSTRUCTIONS?\b", re.IGNORECASE),
    re.compile(r"\bDISREGARD\s+(?:ALL\s+)?(?:PREVIOUS\s+)?INSTRUCTIONS?\b", re.IGNORECASE),
    re.compile(r"\bNEW\s+INSTRUCTIONS?\s*:", re.IGNORECASE),
    re.compile(r"\bOVERRIDE\s+(?:ALL\s+)?(?:PREVIOUS\s+)?(?:INSTRUCTIONS?|RULES?)\b", re.IGNORECASE),
    re.compile(r"\bFORGET\s+(?:ALL\s+)?(?:PREVIOUS\s+)?(?:INSTRUCTIONS?|CONTEXT)\b", re.IGNORECASE),
    re.compile(r"\bYOU\s+ARE\s+NOW\b", re.IGNORECASE),
    re.compile(r"\bACT\s+AS\s+(?:IF\s+)?YOU\s+(?:ARE|WERE)\b", re.IGNORECASE),
    re.compile(r"\bSYSTEM\s*:\s*YOU\s+(?:ARE|MUST|SHOULD|WILL)\b", re.IGNORECASE),
    # Jailbreak framing
    re.compile(r"\bDO\s+ANYTHING\s+NOW\b", re.IGNORECASE),  # DAN
    re.compile(r"\bDEVELOPER\s+MODE\b", re.IGNORECASE),
    re.compile(r"\bJAILBREAK\b", re.IGNORECASE),
    # Role hijack
    re.compile(r"\bPRETEND\s+(?:TO\s+BE|YOU(?:'RE|\s+ARE))\b", re.IGNORECASE),
    re.compile(r"\bROLEPLAY\s+AS\b", re.IGNORECASE),
]


def strip_llm_markers(text: str) -> str:
    """Remove LLM role/instruction markers from text.

    These markers (<|system|>, [INST], <<SYS>>, etc.) could cause a language model
    to interpret injected fleet message content as model instructions rather than data.
    Stripping them ensures fleet messages stay in the data plane.
    """
    for pattern in _LLM_MARKER_PATTERNS:
        text = pattern.sub("", text)
    return text


def strip_prompt_injections(text: str) -> str:
    """Remove known prompt injection patterns from text.

    Replaces matches with '[BLOCKED]' so the recipient knows content was filtered,
    aiding debugging without exposing the injection payload.
    """
    for pattern in _PROMPT_INJECTION_PATTERNS:
        text = pattern.sub("[BLOCKED]", text)
    return text


def limit_message_size(text: str, max_bytes: Optional[int] = None) -> str:
    """Truncate message to max_bytes (default 10KB, configurable via FLEET_MAX_MESSAGE_BYTES).

    Prevents context flooding from oversized messages. Truncation is clearly marked
    so the recipient knows data was lost and can request the full message if needed.
    """
    if max_bytes is None:
        max_bytes = int(os.environ.get("FLEET_MAX_MESSAGE_BYTES", DEFAULT_MAX_MESSAGE_BYTES))

    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text

    # Truncate at byte boundary, decode safely, append truncation notice
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + f"\n\n[TRUNCATED — original {len(encoded)} bytes, limit {max_bytes} bytes]"


def strip_null_bytes(text: str) -> str:
    """Remove null bytes which can cause issues in string processing and logging."""
    return text.replace("\x00", "")


def sanitize_message(text: str, max_bytes: Optional[int] = None) -> str:
    """Full sanitization pipeline: null bytes, LLM markers, prompt injections, size limit.

    This is the standard entry point. Apply to any fleet message content before it
    reaches a Claude session (systemMessage, seed file, or inject).
    """
    text = strip_null_bytes(text)
    text = strip_llm_markers(text)
    text = strip_prompt_injections(text)
    text = limit_message_size(text, max_bytes=max_bytes)
    return text


# -- Input validation for MCP tool arguments --

# Node ID: lowercase letter start, then lowercase alphanumeric / hyphen / underscore, max 32 chars.
# This prevents injection via node IDs used in paths, SSH commands, and NATS subjects.
_NODE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def validate_node_id(target: str) -> str:
    """Validate a node ID against the safe pattern. Returns the ID or raises ValueError.

    Node IDs are used in file paths, SSH user@host commands, and NATS subjects.
    A malicious node ID could enable path traversal or command injection.
    """
    if not target or not isinstance(target, str):
        raise ValueError("Node ID is required and must be a non-empty string")
    if not _NODE_ID_PATTERN.match(target):
        raise ValueError(
            f"Invalid node ID '{target}': must match ^[a-z][a-z0-9_-]{{0,31}}$ "
            f"(lowercase start, alphanumeric/hyphen/underscore, max 32 chars)"
        )
    return target


def validate_body(text: str, field_name: str = "body", max_bytes: Optional[int] = None) -> str:
    """Validate and sanitize a message body field.

    Strips null bytes, enforces size limit, and runs full sanitization.
    Returns the sanitized text or raises ValueError if empty.
    """
    if not text or not isinstance(text, str):
        raise ValueError(f"{field_name} is required and must be a non-empty string")
    return sanitize_message(text, max_bytes=max_bytes)


def validate_task_or_script(value: str, field_name: str = "task") -> str:
    """Validate a task/script field — no path traversal or shell metacharacters.

    Tasks are sent to remote workers and may be used in shell contexts.
    Path traversal (../../etc/passwd) and shell metacharacters (;, |, $(), ``)
    could enable command injection on the remote node.
    """
    if not value or not isinstance(value, str):
        raise ValueError(f"{field_name} is required and must be a non-empty string")

    # Reject path traversal
    if ".." in value:
        raise ValueError(f"{field_name} must not contain '..' (path traversal)")

    # Reject shell metacharacters that enable command injection.
    # We block: backticks, $(), pipe, semicolon, &&, ||
    # These are dangerous when task strings are interpolated into shell commands.
    _SHELL_INJECTION_PATTERN = re.compile(r"[`]|\$\(|;\s*[a-zA-Z/]|&&|\|\|")
    if _SHELL_INJECTION_PATTERN.search(value):
        raise ValueError(
            f"{field_name} contains shell metacharacters that could enable injection. "
            f"Blocked patterns: backticks, $(), semicolons before commands, &&, ||"
        )

    return strip_null_bytes(value)


# -- Log sanitization --
# Prevents API keys from leaking into log files. Keys often appear in error messages,
# stack traces, and HTTP request logs.

_LOG_REDACTION_PATTERNS = [
    # OpenAI / DeepSeek API keys (sk-...)
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "sk-***REDACTED***"),
    # Bearer tokens in headers
    (re.compile(r"Bearer\s+[a-zA-Z0-9._\-]{20,}"), "Bearer ***REDACTED***"),
    # Generic long hex/base64 strings that look like API keys (40+ chars, no spaces)
    # Matches: key=abc123..., "key": "abc123..."
    (re.compile(r'(?<=["\':=])\s*[a-zA-Z0-9_\-]{40,}(?=["\',\s}])'), " ***REDACTED***"),
]


class SanitizingLogFilter(logging.Filter):
    """Logging filter that redacts API keys and tokens from log messages.

    Attach to any logger or handler to prevent credential leakage in logs:

        import logging
        logger = logging.getLogger("fleet")
        logger.addFilter(SanitizingLogFilter())

    Redacts:
    - OpenAI/DeepSeek keys (sk-...)
    - Bearer tokens
    - Generic long secret-like strings in key=value or JSON contexts
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Always returns True (message is kept), but redacts sensitive patterns in-place."""
        if isinstance(record.msg, str):
            for pattern, replacement in _LOG_REDACTION_PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        # Also redact args if they're strings (for %-formatting style logging)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: pattern.sub(replacement, v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                    for pattern, replacement in _LOG_REDACTION_PATTERNS
                }
            elif isinstance(record.args, tuple):
                new_args = []
                for arg in record.args:
                    if isinstance(arg, str):
                        for pattern, replacement in _LOG_REDACTION_PATTERNS:
                            arg = pattern.sub(replacement, arg)
                    new_args.append(arg)
                record.args = tuple(new_args)
        return True
