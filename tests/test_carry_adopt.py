"""Decision 13.15: recovering a lost carry book only through the operator.

/adopt carry (Telegram) asks for a one-time code; /confirm <code> writes a
signed, single-use adopt request; the next carry cycle passes adopt=True to
the plan (min(short, spot) into the book, no trade) and consumes it.
"""

import json

import pytest

from carry import adopt
from helpers import KEY
from telegram_bot import CONFIRM_TTL_S, CommandBot

NOW = 1_790_000_000_000


def test_request_round_trip_and_single_use(tmp_path):
    path = tmp_path / "carry_adopt_request.json"
    assert adopt.read_request(path, KEY, NOW) is None
    adopt.write_request(path, KEY, "telegram chat 42", NOW)
    req = adopt.read_request(path, KEY, NOW + 60_000)
    assert req is not None and req["by"] == "telegram chat 42"
    adopt.consume(path)
    assert adopt.read_request(path, KEY, NOW + 60_000) is None
    adopt.consume(path)                                        # idempotent


def test_request_expires(tmp_path):
    path = tmp_path / "r.json"
    adopt.write_request(path, KEY, "x", NOW)
    assert adopt.read_request(path, KEY, NOW + adopt.ADOPT_TTL_MS + 1) is None


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(requested_ms=d["requested_ms"] + 1),   # tampered after signing
    lambda d: d.update(sig="0" * 64),
    lambda d: d.pop("sig"),
])
def test_tampered_or_unsigned_request_is_refused(tmp_path, mutate):
    path = tmp_path / "r.json"
    adopt.write_request(path, KEY, "x", NOW)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    assert adopt.read_request(path, KEY, NOW) is None


def test_wrong_key_or_garbage_is_refused(tmp_path):
    path = tmp_path / "r.json"
    adopt.write_request(path, KEY, "x", NOW)
    assert adopt.read_request(path, "another-key", NOW) is None
    path.write_text("not json")
    assert adopt.read_request(path, KEY, NOW) is None


# --- Telegram ---------------------------------------------------------------- #

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def msg(text, chat=42, sender=42):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": sender}, "text": text}}


@pytest.fixture
def bot():
    states, adopts, clock = [], [], Clock()
    b = CommandBot("42", lambda s, r: states.append(s), lambda: "status", clock=clock,
                   new_code=lambda: "654321", request_adopt=lambda who: adopts.append(who))
    b.states, b.adopts, b.clock_ = states, adopts, clock
    return b


def test_adopt_carry_needs_a_one_time_code(bot):
    chat, reply = bot.handle(msg("/adopt carry"))
    assert "/confirm 654321" in reply and bot.adopts == []
    chat, reply = bot.handle(msg("/confirm 654321"))
    assert bot.adopts == ["telegram chat 42"] and "✅" in reply
    assert bot.states == []                                    # no risk state written
    assert "Nothing to confirm" in bot.handle(msg("/confirm 654321"))[1]
    assert len(bot.adopts) == 1


def test_adopt_needs_the_carry_word_and_the_right_chat(bot):
    assert "654321" not in bot.handle(msg("/adopt"))[1] and bot.pending is None
    assert bot.handle(msg("/adopt carry", chat=7, sender=7)) is None
    bot.handle(msg("/adopt carry"))
    assert "Wrong code" in bot.handle(msg("/confirm 000000"))[1]
    bot.handle(msg("/adopt carry"))
    bot.clock_.t += CONFIRM_TTL_S + 1
    assert "expired" in bot.handle(msg("/confirm 654321"))[1]
    assert bot.adopts == []


def test_bot_without_carry_does_not_offer_adopt():
    b = CommandBot("42", lambda s, r: None, lambda: "", new_code=lambda: "777")
    assert "777" not in b.handle(msg("/adopt carry"))[1] and b.pending is None
