"""
guardrails.py
Security and input sanitization layer for the Study Material RAG Assistant.
Provides:
- Prompt injection detection for user queries
- Adversarial content scanning for uploaded study documents
- In-memory sliding-window rate limiting
"""

import re
import time
from typing import Tuple, List, Dict

# Known prompt injection & jailbreak heuristic patterns
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules|commands)",
    r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)",
    r"system\s*prompt\s*(override|reset|reveal|leak|bypass)",
    r"you\s+are\s+now\s+(DAN|unfiltered|jailbroken|an\s+evil|in\s+developer\s+mode)",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"\[INST\]",
    r"\[/INST\]",
    r"roleplay\s+as\s+(an\s+unrestricted|a\s+hacked|an\s+evil)",
    r"bypass\s+all\s+(filters|guardrails|safety|guidelines)",
    r"repeat\s+the\s+system\s+prompt\s+verbatim",
    r"what\s+is\s+your\s+secret\s+(key|prompt|instructions)",
    r"pretend\s+you\s+have\s+no\s+rules",
    r"do\s+anything\s+now",
]

COMPILED_INJECTION_REGEX = [re.compile(p, re.IGNORECASE) for p in PROMPT_INJECTION_PATTERNS]


def check_prompt_injection(user_input: str) -> Tuple[bool, str]:
    """
    Checks if a user input string contains prompt injection signatures.
    Returns (is_injected, reason).
    """
    if not user_input or not user_input.strip():
        return False, ""

    normalized = user_input.strip()

    # Pattern check
    for pattern in COMPILED_INJECTION_REGEX:
        match = pattern.search(normalized)
        if match:
            return True, f"Detected potential prompt injection pattern: '{match.group(0)}'"

    # Delimiter anomaly check (e.g. excessive backticks or Markdown system tags)
    if "```system" in normalized.lower() or "### system" in normalized.lower():
        return True, "Detected attempt to simulate system prompt delimiters."

    return False, ""


def scan_document_for_injections(text: str) -> Tuple[bool, List[str]]:
    """
    Scans uploaded document text for embedded malicious prompt instructions.
    Useful for detecting indirect prompt injection in study materials.
    Returns (has_warnings, warning_list).
    """
    warnings = []
    lines = text.split("\n")
    for idx, line in enumerate(lines, start=1):
        clean_line = line.strip()
        if not clean_line:
            continue
        for pattern in COMPILED_INJECTION_REGEX:
            match = pattern.search(clean_line)
            if match:
                warnings.append(
                    f"Line {idx}: Suspicious instruction pattern detected ('{match.group(0)}')"
                )
                if len(warnings) >= 5:
                    break
        if len(warnings) >= 5:
            break

    return len(warnings) > 0, warnings


class RateLimiter:
    """
    Sliding-window in-memory rate limiter per user/key.
    Default: max 20 requests per 60 seconds.
    """
    def __init__(self, max_requests: int = 20, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.history: Dict[str, List[float]] = {}

    def is_allowed(self, user_key: str) -> Tuple[bool, int]:
        """
        Check if user is allowed to proceed.
        Returns (allowed, retry_after_seconds).
        """
        now = time.time()
        user_history = self.history.get(user_key, [])

        # Filter out timestamps outside window
        user_history = [t for t in user_history if now - t < self.window_seconds]
        self.history[user_key] = user_history

        if len(user_history) >= self.max_requests:
            earliest = user_history[0]
            retry_after = int(self.window_seconds - (now - earliest)) + 1
            return False, max(retry_after, 1)

        user_history.append(now)
        self.history[user_key] = user_history
        return True, 0

    def reset(self, user_key: str):
        if user_key in self.history:
            del self.history[user_key]


# Global rate limiter instance
global_rate_limiter = RateLimiter(max_requests=20, window_seconds=60)
