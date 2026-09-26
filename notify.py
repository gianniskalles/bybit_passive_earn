#!/usr/bin/env python3
"""notify.py — the single Telegram sender, with deduplication (T5.2).

Every alert source reports its CURRENT condition on a named channel on
every run; the Notifier decides whether to send:

  condition changed (new problem, different problem) -> send
  condition cleared                                   -> send "resolved"
  same condition, last sent >= REPEAT_S ago           -> send again
  same condition otherwise                            -> nothing

So a stuck state produces one message and a reminder every 6 hours, not
one message per heartbeat. Events (an executed order) bypass dedup.

The chat id comes from config ALERT_TELEGRAM_CHAT_ID (env
ALERT_TELEGRAM_CHAT_ID overrides); the token from TELEGRAM_BOT_TOKEN.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional

import settings

REPEAT_S = 6 * 3600

Sender = Callable[[str, str, str], bool]


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """POST sendMessage. Returns True on HTTP 200; never raises."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def chat_id(env: Dict[str, str], cfg: Optional[dict] = None) -> Optional[str]:
    value = env.get("ALERT_TELEGRAM_CHAT_ID") or (cfg or {}).get("ALERT_TELEGRAM_CHAT_ID")
    return str(value) if value not in (None, "") else None


class Notifier:
    def __init__(self, env: Dict[str, str], cfg: Optional[dict] = None,
                 sender: Optional[Sender] = None, state_file: Optional[Path] = None,
                 clock: Callable[[], float] = time.time):
        self.token = env.get("TELEGRAM_BOT_TOKEN")
        self.chat = chat_id(env, cfg)
        self.sender = sender or send_telegram
        self.state_file = Path(state_file or settings.notify_state_file())
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def _send(self, text: str) -> bool:
        return self.enabled and bool(self.sender(self.token, self.chat, text))

    def observe(self, channel: str, condition: Optional[str], text: str = "",
                resolved_text: Optional[str] = None) -> bool:
        """Report the channel's current condition (None = healthy).
        Returns True if a message was sent."""
        data = self._load()
        prev = data.get(channel) or {}
        now = self.clock()
        last_cond, last_sent = prev.get("condition"), prev.get("sent_at", 0)

        if condition is None:
            if last_cond is None:
                return False
            sent = self._send(resolved_text or f"✅ resolved: {last_cond}")
            data[channel] = {"condition": None, "sent_at": now}
            self._save(data)
            return sent

        if condition == last_cond and now - last_sent < REPEAT_S:
            return False
        sent = self._send(text or condition)
        # A failed send leaves sent_at at 0, so the next run retries.
        data[channel] = {"condition": condition, "sent_at": now if sent else 0}
        self._save(data)
        return sent

    def event(self, text: str) -> bool:
        """Always send (no dedup): e.g. an executed live order."""
        return self._send(text)
