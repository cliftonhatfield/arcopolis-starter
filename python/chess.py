"""Peer chess grammar and finite allowance checks; no action selection or network."""
import math
import re

JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
PEER_CHESS_ACTION_FIELDS = {
    "chess_challenge": ("agentId", "handle", "paceHours"), "chess_respond": ("challengeId", "reply"),
    "chess_resign": ("gameId",), "chess_draw": ("gameId", "reply"), "chess_rematch": ("gameId",),
}
PEER_CHESS_REQUIRED_FIELDS = {
    "chess_challenge": (), "chess_respond": ("challengeId", "reply"), "chess_resign": ("gameId",),
    "chess_draw": ("gameId", "reply"), "chess_rematch": ("gameId",),
}


def peer_chess_action_error(kind, value):
    if kind not in PEER_CHESS_ACTION_FIELDS:
        return None
    if not isinstance(value, dict):
        return "Unknown action or invalid action object."
    if set(value) - set(PEER_CHESS_ACTION_FIELDS[kind]):
        return f"Unexpected field in {kind} action."
    for key in PEER_CHESS_REQUIRED_FIELDS[kind]:
        if not isinstance(value.get(key), str) or not value[key].strip(JS_WHITESPACE):
            return f"Missing required {kind} field."
    for key, field in value.items():
        if key == "paceHours":
            continue
        if not isinstance(field, str) or not field.strip(JS_WHITESPACE):
            return f"{kind}.{key} must be a nonempty string."
        if key.endswith("Id") and not re.fullmatch(r"[A-Za-z0-9_:.-]{1,240}", field.strip(JS_WHITESPACE)):
            return f"{kind}.{key} is not a valid ID."
    if value.get("handle") and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value["handle"].strip(JS_WHITESPACE).lstrip("@").lower()):
        return "Malformed target handle."
    if kind == "chess_challenge":
        if type(value.get("paceHours")) not in (int, float) or value["paceHours"] not in (24, 48):
            return "chess_challenge.paceHours must be 24 or 48."
        if value.get("agentId") and value.get("handle"):
            return "chess_challenge accepts agentId or handle, not both."
    if kind == "chess_respond" and value.get("reply") not in ("accept", "decline", "cancel"):
        return "chess_respond.reply must be accept, decline, or cancel."
    if kind == "chess_draw" and value.get("reply") not in ("offer", "accept", "decline"):
        return "chess_draw.reply must be offer, accept, or decline."
    return None


def is_peer_chess_action(kind, value, data=None):
    """Receipt history is factual context, never evidence of an offered turn."""
    if kind in PEER_CHESS_ACTION_FIELDS:
        return True
    if kind != "chess_move" or not isinstance(value, dict) or not isinstance(value.get("gameId"), str):
        return False
    game_id = value["gameId"].strip(JS_WHITESPACE)
    if game_id.startswith("vchess_"):
        return True
    chess = data.get("chess") if isinstance(data, dict) else None
    return (isinstance(chess, dict) and chess.get("mode") == "shared" and isinstance(chess.get("turns"), list)
            and any(isinstance(turn, dict) and turn.get("gameId") == game_id for turn in chess["turns"]))


def peer_chess_menu_error(data, kind, value):
    if not is_peer_chess_action(kind, value, data):
        return None
    data = data if isinstance(data, dict) else {}
    menu = data.get("menu") if isinstance(data.get("menu"), dict) else {}
    chess = data.get("chess") if isinstance(data.get("chess"), dict) else {}
    if (chess.get("available") is not True or not isinstance(menu.get("actions"), list) or kind not in menu["actions"]
            or not isinstance(menu.get("chessActions"), list) or kind not in menu["chessActions"]):
        return {"code": "ACTION_CLOSED", "message": f"The current menu does not allow {kind}: visitor chess unavailable."}
    budget = menu.get("chessBudget") if isinstance(menu.get("chessBudget"), dict) else {}
    if any(type(budget.get(key)) not in (int, float) or not math.isfinite(budget[key]) or budget[key] <= 0
           for key in ("remaining", "worldRemaining")):
        return {"code": "ACTION_BUDGET_EMPTY", "message": "The current menu has no chess action budget remaining."}
    if kind == "chess_move":
        turns = chess.get("turns") if isinstance(chess.get("turns"), list) else []
        game_id = value.get("gameId", "").strip(JS_WHITESPACE)
        uci = value.get("uci", "").strip(JS_WHITESPACE).lower() if isinstance(value.get("uci"), str) else ""
        legal = any(isinstance(game, dict) and game.get("gameId") == game_id and game.get("yourTurn") is True
                    and isinstance(game.get("legalMoves"), list)
                    and any(isinstance(move, dict) and move.get("uci") == uci for move in game["legalMoves"])
                    for game in turns)
        if not legal:
            return {"code": "INVALID_ACTION", "message": "Choose a legal move offered for your current chess turn."}
    return None
