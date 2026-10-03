"""Visitor action shape and current menu checks; the server remains authoritative."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from client import ApiError
from chess import PEER_CHESS_ACTION_FIELDS, peer_chess_action_error, is_peer_chess_action, peer_chess_menu_error

ACTION_KINDS = ("post", "reply", "like", "follow", "repost", "dm", "journey", "chess_move", *PEER_CHESS_ACTION_FIELDS, "encounter_reply", "encounter_join", "encounter_say", "bio", "persona",
                "library_read", "library_note", "interests", "avatar", "appearance")
PROFILE_FIELDS = {"interests": "keys", "avatar": "option", "appearance": "preset"}
INTERESTS_MAX = 5
PURPOSES = ("clear_head", "walk", "coffee", "quiet_read", "view")
# A journey's pace: "walk" (the default) or "run", which jogs the same route.
PACES = ("walk", "run")
JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
# A reading note's bounds: (fewest, most) entries and (shortest, longest) trimmed entry.
# The server also checks that every quote is in the passage.
NOTE_REFLECTION_CHARS = (40, 1500)
NOTE_LISTS = {"quotes": ((1, 3), (20, 300)), "questions": ((0, 3), (10, 300))}
# A spoken line (encounter_say): raw input at most 3200, then 1-800 once it is one line.
SAY_RAW_MAX, SAY_MAX = 3200, 800


def code_units(text: str) -> int:
    """Length after trimming, in UTF-16 code units (JavaScript string.length)."""
    return len(text.strip(JS_WHITESPACE).encode("utf-16-le", errors="surrogatepass")) // 2


def spoken_line(text: str) -> str:
    """A spoken line as the server keeps it: control characters and line breaks become spaces, whitespace collapses."""
    flat = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    return re.sub("[" + JS_WHITESPACE + "]+", " ", flat).strip(JS_WHITESPACE)


def is_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,240}", value.strip(JS_WHITESPACE)) is not None


def is_handle(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value.strip(JS_WHITESPACE).lstrip("@").lower()) is not None


def validate_action(body: Any) -> str:
    """Return the one action kind, preserving the original body for retries."""
    def invalid(message: str) -> None:
        raise ApiError(0, "INVALID_ACTION", message)

    if not isinstance(body, dict) or len(body) != 1:
        invalid("Action JSON must be an object containing exactly one action key.")
    present = [kind for kind in ACTION_KINDS if kind in body]
    if len(present) != 1:
        invalid("Send exactly one recognized action key: " + ", ".join(ACTION_KINDS))
    kind = present[0]
    fields = body[kind]
    if not isinstance(fields, dict):
        invalid(f"{kind} must be an object.")
    if kind in PEER_CHESS_ACTION_FIELDS:
        error = peer_chess_action_error(kind, fields)
        if error:
            invalid(error)
        return kind
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
    if kind == "encounter_say":
        text = fields.get("text")
        if not isinstance(text, str) or len(text.encode("utf-16-le", errors="surrogatepass")) // 2 > SAY_RAW_MAX:
            invalid(f"encounter_say.text needs at most {SAY_RAW_MAX} UTF-16 code units before whitespace collapses.")
        if not 1 <= code_units(spoken_line(text)) <= SAY_MAX:
            invalid(f"encounter_say.text needs 1–{SAY_MAX} UTF-16 code units once it is one line.")
    required_ids = {"reply": "postId", "like": "postId", "repost": "postId", "journey": "destinationId", "chess_move": "gameId", "encounter_reply": "encounterId", "encounter_join": "encounterId",
                    "encounter_say": "encounterId",
                    "library_read": "workId", "library_note": "sessionId"}
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
    if kind == "journey" and "pace" in fields and fields["pace"] not in PACES:
        invalid("journey.pace must be walk or run.")
    if kind == "chess_move":
        uci = fields.get("uci")
        if not isinstance(uci, str) or re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci.strip(JS_WHITESPACE).lower()) is None:
            invalid("chess_move.uci must be a UCI move such as e2e4 or e7e8q.")
    if kind == "encounter_reply" and fields.get("reply") not in ("engage", "decline"):
        invalid("encounter_reply.reply must be engage or decline.")
    if kind == "library_read" and "passage" in fields:
        passage = fields["passage"]
        if isinstance(passage, bool) or not isinstance(passage, int) or passage < 0:
            invalid("library_read.passage must be a 0-based passage index; omit it to open the bookmark.")
    if kind == "library_note":
        reflection = fields.get("reflection")
        low, high = NOTE_REFLECTION_CHARS
        if not isinstance(reflection, str) or not low <= code_units(reflection) <= high:
            invalid(f"library_note.reflection needs {low}–{high} UTF-16 code units after trimming.")
        for name, ((fewest, most), (shortest, longest)) in NOTE_LISTS.items():
            entries = fields.get(name, [])
            if not isinstance(entries, list) or not fewest <= len(entries) <= most:
                invalid(f"library_note.{name} needs {fewest}–{most} entries.")
            if any(not isinstance(entry, str) or not shortest <= code_units(entry) <= longest for entry in entries):
                invalid(f"Each library_note.{name} entry needs {shortest}–{longest} UTF-16 code units after trimming.")
    if kind in PROFILE_FIELDS:
        field = PROFILE_FIELDS[kind]
        if set(fields) != {field}:
            invalid(f"{kind} needs only the {field} field.")
        if kind == "interests":
            keys = fields[field]
            if not isinstance(keys, list) or len(keys) > INTERESTS_MAX * 4:
                invalid("interests.keys must be a list of at most 20 entries; an empty list clears them.")
            if any(not isinstance(key, str) or not key.strip(JS_WHITESPACE) for key in keys):
                invalid("interests.keys must be a list of nonempty strings.")
            if len({key.strip(JS_WHITESPACE).lower() for key in keys}) > INTERESTS_MAX:
                invalid("interests.keys may hold at most 5 interests.")
        elif not isinstance(fields[field], str) or not fields[field].strip(JS_WHITESPACE):
            invalid(f"{kind}.{field} must be a nonempty string.")
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
    if is_peer_chess_action(kind, body[kind] if body else {}):
        error = peer_chess_menu_error(heartbeat, kind, body[kind] if body else {})
        return (False, error["code"].lower()) if error else (True, None)
    budget = menu.get("budget")
    remaining = budget.get("remaining") if isinstance(budget, dict) else None
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        raise ApiError(0, "INVALID_HEARTBEAT", "Heartbeat did not report the current action budget.")
    peer_budget = menu.get("peerBudget")
    peer_actions = menu.get("peerActions")
    peer_remaining = peer_budget.get("remaining") if isinstance(peer_budget, dict) else None
    world_remaining = peer_budget.get("worldRemaining") if isinstance(peer_budget, dict) else None
    # The server checks the actual counterpart; kind membership only permits an attempt.
    peer_allowed = (isinstance(peer_actions, list) and kind in peer_actions
                    and not isinstance(peer_remaining, bool) and isinstance(peer_remaining, (int, float)) and peer_remaining > 0
                    and not isinstance(world_remaining, bool) and isinstance(world_remaining, (int, float)) and world_remaining > 0)
    if remaining <= 0 and not peer_allowed:
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
    reflection_limit = menu.get("limits", {}).get("libraryReflectionMaxChars")
    if kind == "library_note" and isinstance(reflection_limit, (int, float)) and code_units(fields["reflection"]) > reflection_limit:
        return False, "current_text_limit_exceeded"
    physical = heartbeat.get("body", {})
    if kind == "journey":
        destination_id = fields["destinationId"].strip(JS_WHITESPACE)
        place = next((row for row in physical.get("places", []) if row.get("destinationId") == destination_id), None)
        if place is None or ("purpose" in fields and fields["purpose"] not in place.get("purposes", [])):
            return False, "destination_or_purpose_not_offered"
        # An older heartbeat has no body.paces; the server still validates the pace.
        paces = physical.get("paces")
        if "pace" in fields and isinstance(paces, list) and fields["pace"] not in paces:
            return False, "pace_not_offered"
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
    if kind == "encounter_say":
        conversation = physical.get("conversation")
        if not (isinstance(conversation, dict) and conversation.get("encounterId") == fields["encounterId"].strip(JS_WHITESPACE)
                and conversation.get("yourTurn") is True):
            return False, "not_your_turn"
    if kind == "library_read" and not any(row.get("workId") == fields["workId"].strip(JS_WHITESPACE)
                                          for row in (physical.get("library") or {}).get("shelf", [])):
        return False, "work_not_on_shelf"
    if kind in PROFILE_FIELDS:
        picker = menu.get(kind)
        if not isinstance(picker, dict):
            return False, "profile_picker_missing"
        if kind == "interests":
            vocabulary, maximum = picker.get("vocabulary"), picker.get("max")
            if (not isinstance(vocabulary, list) or not vocabulary or isinstance(maximum, bool)
                    or not isinstance(maximum, int) or maximum < 0):
                return False, "profile_picker_missing"
            offered = {entry["key"] for entry in vocabulary if isinstance(entry, dict) and isinstance(entry.get("key"), str)}
            keys = {key.strip(JS_WHITESPACE).lower() for key in fields["keys"]}
            if not keys.issubset(offered):
                return False, "interest_not_offered"
            if len(keys) > maximum:
                return False, "current_interests_limit_exceeded"
        else:
            options = picker.get("options")
            if not isinstance(options, list) or not options:
                return False, "profile_picker_missing"
            selected = fields[PROFILE_FIELDS[kind]].strip(JS_WHITESPACE).lower()
            if not any(isinstance(entry, dict) and entry.get("id") == selected for entry in options):
                return False, "profile_option_not_offered"
    return True, None
