#!/usr/bin/env python3
"""risk_state.py — read, write and verify the signed risk_state.json.

Record format (all fields signed, via signing.py — the single HMAC source):

  {"profile": "hermes-yield-rotation",
   "state":   "NORMAL" | "NO_NEW_POSITIONS" | "UNWIND",
   "ts":      <int, epoch ms>,
   "reason":  <str>,
   "source":  "heartbeat_bootstrap" | "heartbeat_renew" | "operator",
   "sig":     <hex HMAC-SHA256 over the other fields>}

verify() never raises and never returns free text to branch on: it returns
a Verification whose `code` is one of the CODE_* constants below, plus the
two independent facts callers need — is the signature valid, is it fresh.

CLI (reads the key via settings.load_env, the path via settings):
  python risk_state.py verify
  python risk_state.py write <STATE> <reason...>      # source = operator
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import settings
from signing import sign, verify as verify_sig

PROFILE = "hermes-yield-rotation"
STATES = ("NORMAL", "NO_NEW_POSITIONS", "UNWIND")
SOURCE_BOOTSTRAP = "heartbeat_bootstrap"
SOURCE_RENEW = "heartbeat_renew"
SOURCE_OPERATOR = "operator"
SOURCES = (SOURCE_BOOTSTRAP, SOURCE_RENEW, SOURCE_OPERATOR)

MAX_AGE_MS = 30 * 60 * 1000       # a record older than this is stale
MAX_FUTURE_SKEW_MS = 60 * 1000    # tolerated clock skew for ts in the future

CODE_OK = "OK"
CODE_STALE = "RISK_STATE_STALE"              # signature valid, too old
CODE_MISSING = "RISK_STATE_MISSING"          # no file
CODE_UNREADABLE = "RISK_STATE_UNREADABLE"    # I/O error or not JSON
CODE_MALFORMED = "RISK_STATE_MALFORMED"      # JSON, but wrong shape/types
CODE_BAD_SIGNATURE = "RISK_STATE_BAD_SIGNATURE"
CODE_FUTURE_TS = "RISK_STATE_FUTURE_TS"      # signed, but ts in the future
CODE_NO_KEY = "RISK_STATE_NO_KEY"            # HMAC key not configured

_FIELDS = ("profile", "state", "ts", "reason", "source", "sig")


@dataclass(frozen=True)
class Verification:
    code: str
    signature_valid: bool          # well-formed AND HMAC matches
    fresh: bool                    # signature_valid AND age within MAX_AGE_MS
    state: Optional[str] = None    # only set when signature_valid
    source: Optional[str] = None   # only set when signature_valid
    ts: Optional[int] = None       # only set when signature_valid
    reason: Optional[str] = None   # only set when signature_valid
    age_ms: Optional[int] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code == CODE_OK

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_ms() -> int:
    return int(time.time() * 1000)


def make_record(secret: str, state: str, reason: str, source: str,
                ts_ms: Optional[int] = None) -> Dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"invalid state {state!r}; expected one of {STATES}")
    if source not in SOURCES:
        raise ValueError(f"invalid source {source!r}; expected one of {SOURCES}")
    if not secret:
        raise ValueError("HMAC secret is empty")
    obj = {
        "profile": PROFILE,
        "state": state,
        "ts": _now_ms() if ts_ms is None else int(ts_ms),
        "reason": str(reason),
        "source": source,
    }
    obj["sig"] = sign(secret, obj)
    return obj


def write(path: Path, secret: str, state: str, reason: str, source: str,
          ts_ms: Optional[int] = None) -> Dict[str, Any]:
    """Atomically write a signed record (tmp + replace). Returns the record."""
    obj = make_record(secret, state, reason, source, ts_ms)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, path)
    return obj


def _shape_problem(obj: Any) -> Optional[str]:
    if not isinstance(obj, dict):
        return f"top-level JSON is {type(obj).__name__}, expected object"
    missing = [f for f in _FIELDS if f not in obj]
    if missing:
        return f"missing fields: {missing}"
    extra = sorted(set(obj) - set(_FIELDS))
    if extra:
        return f"unexpected fields: {extra}"
    if not isinstance(obj["sig"], str):
        return "sig is not a string"
    if not isinstance(obj["ts"], int) or isinstance(obj["ts"], bool):
        return "ts is not an integer"
    for f in ("profile", "state", "reason", "source"):
        if not isinstance(obj[f], str):
            return f"{f} is not a string"
    if obj["profile"] != PROFILE:
        return f"profile {obj['profile']!r} != {PROFILE!r}"
    if obj["state"] not in STATES:
        return f"invalid state {obj['state']!r}"
    if obj["source"] not in SOURCES:
        return f"invalid source {obj['source']!r}"
    return None


def verify(path: Path, secret: str, now_ms: Optional[int] = None,
           max_age_ms: int = MAX_AGE_MS) -> Verification:
    """Verify the file at `path`. Never raises."""
    if not secret:
        return Verification(CODE_NO_KEY, False, False, detail="HMAC key not set")
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Verification(CODE_MISSING, False, False, detail=f"missing: {path}")
    except (OSError, UnicodeDecodeError) as e:
        return Verification(CODE_UNREADABLE, False, False, detail=f"unreadable: {e}")
    try:
        obj = json.loads(text)
    except ValueError as e:
        return Verification(CODE_UNREADABLE, False, False, detail=f"not JSON: {e}")

    problem = _shape_problem(obj)
    if problem:
        return Verification(CODE_MALFORMED, False, False, detail=problem)

    payload = {k: v for k, v in obj.items() if k != "sig"}
    if not verify_sig(secret, payload, obj["sig"]):
        return Verification(CODE_BAD_SIGNATURE, False, False, detail="HMAC mismatch")

    now = _now_ms() if now_ms is None else now_ms
    age = now - obj["ts"]
    signed = dict(state=obj["state"], source=obj["source"], ts=obj["ts"],
                  reason=obj["reason"], age_ms=age)
    if age < -MAX_FUTURE_SKEW_MS:
        return Verification(CODE_FUTURE_TS, True, False, **signed,
                            detail=f"ts is {-age // 1000}s in the future")
    if age > max_age_ms:
        return Verification(CODE_STALE, True, False, **signed,
                            detail=f"stale ({age // 1000}s old)")
    return Verification(CODE_OK, True, True, **signed)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = settings.load_env()
    secret = env.get("HERMES_RISK_HMAC_KEY", "")
    path = settings.risk_state_file()
    if argv[:1] == ["verify"] and len(argv) == 1:
        v = verify(path, secret)
        print(json.dumps(v.as_dict(), indent=2))
        return 0 if v.ok else 1
    if argv[:1] == ["write"] and len(argv) >= 3:
        obj = write(path, secret, argv[1], " ".join(argv[2:]), SOURCE_OPERATOR)
        print(json.dumps(obj, indent=2))
        return 0
    print(__doc__.split("CLI", 1)[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
