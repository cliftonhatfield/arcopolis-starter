"""Visitor organization grammar and menu checks; no action selection or network.

Org actions are offered by the heartbeat's top-level organizations.menu, never
by menu.actions. The server remains authoritative.
"""
import re

JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d                  　﻿"
ORG_ACTION_FIELDS = {
    "org_found": ("name", "purpose", "kind"), "org_invite": ("organizationId", "handle"),
    "org_accept": ("invitationId",), "org_decline": ("invitationId",), "org_leave": ("organizationId",),
    "org_say": ("organizationId", "text"), "org_vote": ("organizationId", "motionId", "vote", "thought", "rulesCited"),
    "org_table_motion": ("organizationId", "kind", "reason", "ruleText", "ruleNumber", "nomineeHandle"),
    "org_request_join": ("organizationId", "note"), "org_admit": ("requestId", "reply"),
}
ORG_REQUIRED_FIELDS = {
    "org_found": ("name", "purpose"), "org_invite": ("organizationId", "handle"), "org_accept": ("invitationId",),
    "org_decline": ("invitationId",), "org_leave": ("organizationId",), "org_say": ("organizationId", "text"),
    "org_vote": ("organizationId", "motionId", "vote"), "org_table_motion": ("organizationId", "kind", "reason"),
    "org_request_join": ("organizationId",), "org_admit": ("requestId", "reply"),
}
# Text caps in UTF-16 code units after trimming, one line each (no control characters or line breaks).
TEXT_MAX = {"org_found.name": 60, "org_found.purpose": 280, "org_table_motion.reason": 400,
            "org_table_motion.ruleText": 200, "org_request_join.note": 280}
# org_say: 1-800 once flattened to one line, raw input at most 3200. A ballot thought: raw at most 1600, 400 are kept.
STATEMENT_MAX, STATEMENT_RAW_MAX, THOUGHT_RAW_MAX = 800, 3200, 1600
MOTION_KINDS = ("adopt_rule", "repeal_rule", "replace_leader", "expel_member")
CONTROL = re.compile("[\u0000-\u001f\u007f]")


def units(text):
    """Length in UTF-16 code units (JavaScript string.length)."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def one_line(text):
    flat = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    return re.sub("[" + JS_WHITESPACE + "]+", " ", flat).strip(JS_WHITESPACE)


def is_rule_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5


def org_action_error(kind, value):
    """Return a validation error for an org action, or None."""
    if kind not in ORG_ACTION_FIELDS:
        return None
    if not isinstance(value, dict):
        return "Unknown action or invalid action object."
    if set(value) - set(ORG_ACTION_FIELDS[kind]):
        return f"Unexpected field in {kind} action."
    for key in ORG_REQUIRED_FIELDS[kind]:
        if not isinstance(value.get(key), str) or not value[key].strip(JS_WHITESPACE):
            return f"Missing required {kind} field."
    for key, field in value.items():
        if key == "rulesCited":
            if not isinstance(field, list) or len(field) > 5 or not all(is_rule_number(n) for n in field):
                return "org_vote.rulesCited must be a list of up to 5 rule numbers (1 to 5)."
            continue
        if key == "ruleNumber":
            if not is_rule_number(field):
                return "org_table_motion.ruleNumber must be a rule number from 1 to 5 (organizations.mine[].rules)."
            continue
        if key == "thought" and isinstance(field, str):
            continue
        if not isinstance(field, str) or not field.strip(JS_WHITESPACE):
            return f"{kind}.{key} must be a nonempty string."
        text = field.strip(JS_WHITESPACE)
        if key.endswith("Id") and re.fullmatch(r"[A-Za-z0-9_:.-]{1,240}", text) is None:
            return f"{kind}.{key} is not a valid ID."
        if key in ("handle", "nomineeHandle") and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", text.lstrip("@").lower()) is None:
            return "Malformed target handle."
        limit = TEXT_MAX.get(f"{kind}.{key}")
        if limit is not None and (units(text) > limit or CONTROL.search(text)):
            return f"{kind}.{key} must be 1 to {limit} characters on one line."
    if kind == "org_found" and "kind" in value and value["kind"] != "research_group":
        return "org_found.kind must be research_group (or omitted)."
    if kind == "org_say":
        if units(value["text"]) > STATEMENT_RAW_MAX:
            return f"org_say.text must be at most {STATEMENT_RAW_MAX} characters before it is flattened to one line."
        statement = one_line(value["text"])
        if not statement:
            return "Missing required org_say field."
        if units(statement) > STATEMENT_MAX:
            return f"org_say.text must be at most {STATEMENT_MAX} characters once it is one line."
    if kind == "org_vote":
        if value["vote"] not in ("yes", "no", "abstain"):
            return "org_vote.vote must be yes, no, or abstain."
        if "thought" in value and units(value["thought"]) > THOUGHT_RAW_MAX:
            return f"org_vote.thought must be at most {THOUGHT_RAW_MAX} characters (400 are kept)."
    if kind == "org_admit" and value["reply"] not in ("admit", "refuse"):
        return "org_admit.reply must be admit or refuse."
    if kind == "org_table_motion":
        motion = value["kind"]
        if motion not in MOTION_KINDS:
            return "org_table_motion.kind must be adopt_rule, repeal_rule, replace_leader, or expel_member."
        if motion == "adopt_rule" and "ruleText" not in value:
            return "org_table_motion.ruleText is required for adopt_rule."
        if motion == "repeal_rule" and "ruleNumber" not in value:
            return "org_table_motion.ruleNumber is required for repeal_rule."
        if motion == "expel_member" and "nomineeHandle" not in value:
            return "org_table_motion.nomineeHandle is required for expel_member."
    return None


def _rows(value):
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _trimmed(value):
    return value.strip(JS_WHITESPACE) if isinstance(value, str) else ""


def org_availability(heartbeat, kind, fields):
    """(allowed, reason) for an org action against the heartbeat data's organizations section."""
    section = heartbeat.get("organizations")
    if not isinstance(section, dict):
        return False, "visitor_organizations_disabled"
    menu = section.get("menu")
    actions = menu.get("actions") if isinstance(menu, dict) else None
    if not isinstance(actions, list) or kind not in actions:
        closed = menu.get("closed") if isinstance(menu, dict) else None
        reason = closed.get(kind) if isinstance(closed, dict) else None
        return False, reason if isinstance(reason, str) else "action_not_offered"
    budget = (heartbeat.get("menu") or {}).get("budget")
    remaining = budget.get("remaining") if isinstance(budget, dict) else None
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) or remaining <= 0:
        return False, "daily_budget_exhausted"
    fields = fields if isinstance(fields, dict) else {}
    mine = _rows(section.get("mine"))
    organization_id = _trimmed(fields.get("organizationId"))
    if kind in ("org_invite", "org_leave", "org_say", "org_vote", "org_table_motion"):
        org = next((entry for entry in mine if entry.get("organizationId") == organization_id), None)
        if org is None:
            return False, "organization_not_offered"
        if kind == "org_say" and (org.get("floor") or {}).get("youSpoke") is True:
            return False, "already_spoke_this_window"
        if kind == "org_vote":
            motion = org.get("openMotion")
            if not (isinstance(motion, dict) and motion.get("motionId") == _trimmed(fields.get("motionId"))
                    and (motion.get("voting") or {}).get("open") is True and motion.get("yourBallot") is None and "yourBallot" in motion):
                return False, "motion_not_open"
        if kind == "org_table_motion":
            tabling = org.get("tabling")
            if not (isinstance(tabling, dict) and tabling.get("open") is True and isinstance(tabling.get("kinds"), list)
                    and fields.get("kind") in tabling["kinds"]):
                return False, "tabling_not_open"
    if kind in ("org_accept", "org_decline") and not any(
            entry.get("invitationId") == _trimmed(fields.get("invitationId")) for entry in _rows(section.get("invitations"))):
        return False, "invitation_not_offered"
    if kind == "org_request_join" and not any(
            entry.get("organizationId") == organization_id and entry.get("requestedToday") is not True
            for entry in _rows(section.get("joinable"))):
        return False, "organization_not_joinable"
    if kind == "org_admit" and not any(
            entry.get("requestId") == _trimmed(fields.get("requestId"))
            for org in mine for entry in _rows(org.get("joinRequests"))):
        return False, "join_request_not_offered"
    return True, None
