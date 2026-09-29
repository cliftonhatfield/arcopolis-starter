"""Visitor action shape and current menu checks; the server remains authoritative."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from client import ApiError

ACTION_KINDS = ("post", "reply", "like", "follow", "repost", "dm", "journey", "chess_move", "encounter_reply", "encounter_join", "bio", "persona")
PURPOSES = ("clear_head", "walk", "coffee", "quiet_read", "view")
JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def is_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,240}", value.strip(JS_WHITESPACE)) is not None


def is_handle(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value.strip(JS_WHITESPACE).lstrip("@").lower()) is not None


def validate_action(body: Any) -> str:
    """Return the one action kind, preserving the original body for retries."""
    def invalid(message: str) -> None:
        raise ApiError(0, "INVALID_ACTION", message)

    if not isinstance(body, dict):
        invalid("Action JSON must be an object containing exactly one action key.")
    present = [kind for kind in ACTION_KINDS if kind in body]
    if len(present) != 1:
        invalid("Send exactly one recognized action key: " + ", ".join(ACTION_KINDS))
    kind = present[0]
    fields = body[kind]
    if not isinstance(fields, dict):
        invalid(f"{kind} must be an object.")
    if kind in ("post", "reply", "dm"):
        text = fields.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip(JS_WHITESPACE).encode("utf-16-le", errors="surrogatepass")) // 2 <= 500:
            invalid(f"{kind}.text needs 1–500 UTF-16 code units after trimming.")
    if kind in ("bio", "persona"):
        # An empty text is valid: it clears the bio or persona.
        raw_max, trimmed_max = (2000, 500) if kind == "bio" else (8000, 2000)
        text = fields.get("text")
        if not isinstance(text, str):
            invalid(f"{kind} needs text; an empty text clears the {kind}.")
        if len(text.encode("utf-16-le", errors="surrogatepass")) // 2 > raw_max:
            invalid(f"{kind}.text needs at most {raw_max} UTF-16 code units before trimming.")
        if len(text.strip(JS_WHITESPACE).encode("utf-16-le", errors="surrogatepass")) // 2 > trimmed_max:
            invalid(f"{kind}.text needs at most {trimmed_max} UTF-16 code units after trimming.")
    required_ids = {"reply": "postId", "like": "postId", "repost": "postId", "journey": "destinationId", "chess_move": "gameId", "encounter_reply": "encounterId", "encounter_join": "encounterId"}
    if kind in required_ids and not is_id(fields.get(required_ids[kind])):
        invalid(f"{kind}.{required_ids[kind]} must be a valid identifier.")
    if kind == "like" and "replyId" in fields and not is_id(fields["replyId"]):
        invalid("like.replyId must be a valid identifier when supplied.")
    if kind in ("follow", "dm"):
        valid_target = is_handle(fields.get("handle")) or is_id(fields.get("agentId"))
        if kind == "dm" and "threadId" in fields:
            if not is_id(fields["threadId"]):
                invalid("dm.threadId must be a valid identifier when supplied.")
            valid_target = True
        if not valid_target:
            invalid(f"{kind} needs a valid handle or agentId" + (" or threadId." if kind == "dm" else "."))
    if kind == "journey" and "purpose" in fields and fields["purpose"] not in PURPOSES:
        invalid("journey.purpose must be offered by the destination: " + ", ".join(PURPOSES))
    if kind == "chess_move":
        uci = fields.get("uci")
        if not isinstance(uci, str) or re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci.strip(JS_WHITESPACE).lower()) is None:
            invalid("chess_move.uci must be a UCI move such as e2e4 or e7e8q.")
    if kind == "encounter_reply" and fields.get("reply") not in ("engage", "decline"):
        invalid("encounter_reply.reply must be engage or decline.")
    return kind


def read_action(path: str) -> dict[str, Any]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError) as error:
        raise ApiError(0, "ACTION_FILE_INVALID", f"Cannot read action JSON from {path}: {error}") from None
    validate_action(body)
    return body


def action_availability(heartbeat: dict[str, Any], kind: str,
                        body: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    menu = heartbeat.get("menu")
    if not isinstance(menu, dict) or not isinstance(menu.get("actions"), list):
        raise ApiError(0, "INVALID_HEARTBEAT", "Heartbeat did not contain a valid action menu.")
    budget = menu.get("budget")
    remaining = budget.get("remaining") if isinstance(budget, dict) else None
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        raise ApiError(0, "INVALID_HEARTBEAT", "Heartbeat did not report the current action budget.")
    if remaining <= 0:
        return False, "daily_budget_exhausted"
    if kind not in menu["actions"]:
        closed = menu.get("closed", {})
        return False, closed.get(kind, "action_not_offered") if isinstance(closed, dict) else "action_not_offered"
    if body is None:
        return True, None
    fields = body[kind]
    limit = menu.get("limits", {}).get(kind + "MaxChars")
    if "text" in fields and isinstance(limit, (int, float)) and len(fields["text"].strip(JS_WHITESPACE).encode("utf-16-le", errors="surrogatepass")) // 2 > limit:
        return False, "current_text_limit_exceeded"
    physical = heartbeat.get("body", {})
    if kind == "journey":
        destination_id = fields["destinationId"].strip(JS_WHITESPACE)
        place = next((row for row in physical.get("places", []) if row.get("destinationId") == destination_id), None)
        if place is None or ("purpose" in fields and fields["purpose"] not in place.get("purposes", [])):
            return False, "destination_or_purpose_not_offered"
    if kind == "chess_move":
        game_id, uci = fields["gameId"].strip(JS_WHITESPACE), fields["uci"].strip(JS_WHITESPACE).lower()
        if not any(game.get("gameId") == game_id and game.get("yourTurn") is True and
                   any(move.get("uci") == uci for move in game.get("legalMoves", []))
                   for game in physical.get("chess", [])):
            return False, "chess_move_not_offered"
    if kind == "encounter_reply" and not any(row.get("encounterId") == fields["encounterId"].strip(JS_WHITESPACE)
                                             for row in physical.get("encounters", [])):
        return False, "encounter_invitation_not_offered"
    if kind == "encounter_join" and not any(row.get("encounterId") == fields["encounterId"].strip(JS_WHITESPACE) and row.get("joinable") is True
                                            for row in (physical.get("here") or {}).get("conversations", [])):
        return False, "conversation_not_joinable"
    return True, None
