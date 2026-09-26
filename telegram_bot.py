#!/usr/bin/env python3
"""telegram_bot.py — operator commands over Telegram (T5.3).

  /status          current risk_state (verified)
  /unwind          ask to set UNWIND   -> reply with a one-time code
  /resume          ask to set NORMAL   -> reply with a one-time code
  /confirm <code>  second message: writes the state with source=operator
  /cancel          drop the pending request

Only messages whose chat.id AND from.id equal the configured
ALERT_TELEGRAM_CHAT_ID are accepted; everything else is ignored without a
reply. A confirmation code is valid for CONFIRM_TTL_S and for one use.
The heartbeat never overrides a source=operator state.

Uses its OWN bot token, YIELD_TELEGRAM_BOT_TOKEN (from /opt/hermes/.env):
long-polling getUpdates on the shared TELEGRAM_BOT_TOKEN would conflict
with whichever other service already polls it. Commands queued while the
bot was down are discarded at start-up, never replayed.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional, Tuple

import yaml

import risk_state
import settings
from notify import chat_id as configured_chat_id

CONFIRM_TTL_S = 120
TARGETS = {"/unwind": "UNWIND", "/resume": "NORMAL"}
HELP = "Commands: /status, /unwind, /resume, /confirm <code>, /cancel"


class CommandBot:
    def __init__(self, allowed_chat_id: str,
                 write_state: Callable[[str, str], None],
                 read_status: Callable[[], str],
                 clock: Callable[[], float] = time.time,
                 new_code: Callable[[], str] = lambda: f"{secrets.randbelow(10**6):06d}"):
        self.allowed = str(allowed_chat_id)
        self.write_state = write_state
        self.read_status = read_status
        self.clock = clock
        self.new_code = new_code
        self.pending: Optional[Dict[str, object]] = None

    def handle(self, update: Dict) -> Optional[Tuple[str, str]]:
        """Return (chat_id, reply) or None (ignored)."""
        msg = update.get("message") if isinstance(update, dict) else None
        if not isinstance(msg, dict):
            return None
        chat = str((msg.get("chat") or {}).get("id"))
        sender = str((msg.get("from") or {}).get("id"))
        if chat != self.allowed or sender != self.allowed:
            print(f"[telegram_bot] ignored message from chat={chat} from={sender}", file=sys.stderr)
            return None
        words = str(msg.get("text") or "").strip().split()
        if not words:
            return chat, HELP
        cmd = words[0].split("@", 1)[0].lower()

        if cmd == "/status":
            return chat, self.read_status()
        if cmd in TARGETS:
            code = self.new_code()
            self.pending = {"state": TARGETS[cmd], "code": code,
                            "expires": self.clock() + CONFIRM_TTL_S}
            return chat, (f"Set risk state to {TARGETS[cmd]}? Reply /confirm {code} within "
                          f"{CONFIRM_TTL_S // 60} min, or /cancel.")
        if cmd == "/cancel":
            self.pending = None
            return chat, "Cancelled."
        if cmd == "/confirm":
            pending, self.pending = self.pending, None  # one use, whatever happens
            if pending is None:
                return chat, "Nothing to confirm."
            if self.clock() > pending["expires"]:
                return chat, "Confirmation expired. Send the command again."
            if len(words) != 2 or not secrets.compare_digest(words[1], str(pending["code"])):
                return chat, "Wrong code. Send the command again."
            state = str(pending["state"])
            try:
                self.write_state(state, f"telegram operator command (chat {chat})")
            except Exception as e:
                return chat, f"❌ Could not write risk state: {e}"
            return chat, f"✅ Risk state set to {state} (source=operator).\n{self.read_status()}"
        return chat, HELP


# --------------------------------------------------------------------------- #
# Telegram transport                                                          #
# --------------------------------------------------------------------------- #

def _api(token: str, method: str, params: Dict, timeout: int = 40) -> Dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=timeout) as r:
        payload = json.loads(r.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method}: {payload}")
    return payload


def main() -> int:
    env = settings.load_env()
    cfg = yaml.safe_load(settings.config_file().read_text()) or {}
    token = env.get("YIELD_TELEGRAM_BOT_TOKEN")
    allowed = configured_chat_id(env, cfg)
    key = env.get("HERMES_RISK_HMAC_KEY")
    if not (token and allowed and key):
        print("[telegram_bot] YIELD_TELEGRAM_BOT_TOKEN, ALERT_TELEGRAM_CHAT_ID and "
              "HERMES_RISK_HMAC_KEY are required", file=sys.stderr)
        return 2
    state_file = settings.risk_state_file()

    def write_state(state: str, reason: str) -> None:
        risk_state.write(state_file, key, state, reason, risk_state.SOURCE_OPERATOR)

    def read_status() -> str:
        v = risk_state.verify(state_file, key)
        age = f"{v.age_ms // 60000} min" if v.age_ms is not None else "?"
        return f"risk_state: {v.state or '-'} ({v.code}, source={v.source}, age {age})"

    bot = CommandBot(allowed, write_state, read_status)
    # Discard anything queued while the bot was down.
    backlog = _api(token, "getUpdates", {"offset": -1, "timeout": 0}).get("result", [])
    offset = backlog[-1]["update_id"] + 1 if backlog else 0
    print("[telegram_bot] started", file=sys.stderr)
    while True:
        try:
            updates = _api(token, "getUpdates", {"offset": offset, "timeout": 30}).get("result", [])
        except Exception as e:
            print(f"[telegram_bot] getUpdates failed: {e}", file=sys.stderr)
            time.sleep(10)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            reply = bot.handle(update)
            if reply:
                try:
                    _api(token, "sendMessage", {"chat_id": reply[0], "text": reply[1]}, timeout=10)
                except Exception as e:
                    print(f"[telegram_bot] sendMessage failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
