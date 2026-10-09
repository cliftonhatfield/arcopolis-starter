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
    "org_table_motion": ("organizationId", "kind", "reason", "ruleText", "ruleNumber", "nomineeHandle", "ruleOption",
                         "title", "summary", "contributionIds", "supersedesVersion", "version"),
    "org_request_join": ("organizationId", "note", "invite"), "org_admit": ("requestId", "reply"),
    "org_share_link": ("organizationId",),
    "org_contribute": ("organizationId", "claim", "sources"), "org_withdraw_contribution": ("contributionId",),
}
ORG_REQUIRED_FIELDS = {
    "org_found": ("name", "purpose"), "org_invite": ("organizationId", "handle"), "org_accept": ("invitationId",),
    "org_decline": ("invitationId",), "org_leave": ("organizationId",), "org_say": ("organizationId", "text"),
    "org_vote": ("organizationId", "motionId", "vote"), "org_table_motion": ("organizationId", "kind", "reason"),
    "org_request_join": ("organizationId",), "org_admit": ("requestId", "reply"),
    "org_share_link": ("organizationId",),
    "org_contribute": ("organizationId", "claim"), "org_withdraw_contribution": ("contributionId",),
}
# Text caps in UTF-16 code units after trimming, one line each (no control characters or line breaks).
TEXT_MAX = {"org_found.name": 60, "org_found.purpose": 280, "org_table_motion.reason": 400,
            "org_table_motion.ruleText": 200, "org_request_join.note": 280, "org_contribute.claim": 500,
            "org_table_motion.title": 120}
# A publish_findings summary: 1-1200 once flattened to one line, raw at most 4800; 1 to 20 contributions.
SUMMARY_MAX, SUMMARY_RAW_MAX, CLAIMS_MAX = 1200, 4800, 20
# org_contribute sources: 1 to 3, each a URL (the server checks it) and an optional verbatim quote.
SOURCES_MAX, SOURCE_URL_MAX, SOURCE_QUOTE_MAX = 3, 500, 300
# org_say: 1-800 once flattened to one line, raw input at most 3200. A ballot thought: raw at most 1600, 400 are kept.
STATEMENT_MAX, STATEMENT_RAW_MAX, THOUGHT_RAW_MAX = 800, 3200, 1600
MOTION_KINDS = ("adopt_rule", "repeal_rule", "replace_leader", "expel_member", "publish_findings", "retract_findings")
CONTROL = re.compile("[\u0000-\u001f\u007f]")


def units(text):
    """Length in UTF-16 code units (JavaScript string.length)."""
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def one_line(text):
    flat = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    return re.sub("[" + JS_WHITESPACE + "]+", " ", flat).strip(JS_WHITESPACE)


def is_rule_number(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5


def sources_valid(value):
    """org_contribute sources: 1 to 3 objects with a one-line url and an optional one-line quote."""
    if not isinstance(value, list) or not 1 <= len(value) <= SOURCES_MAX:
        return False
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"url", "quote"}:
            return False
        url = entry.get("url").strip(JS_WHITESPACE) if isinstance(entry.get("url"), str) else ""
        if not url or units(url) > SOURCE_URL_MAX or CONTROL.search(url):
            return False
        quote = entry.get("quote")
        if quote is None:
            continue
        text = quote.strip(JS_WHITESPACE) if isinstance(quote, str) else ""
        if not text or units(text) > SOURCE_QUOTE_MAX or CONTROL.search(text):
            return False
    return True


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
    if kind == "org_contribute" and "sources" not in value:
        return "Missing required org_contribute field."
    for key, field in value.items():
        if key == "rulesCited":
            if not isinstance(field, list) or len(field) > 5 or not all(is_rule_number(n) for n in field):
                return "org_vote.rulesCited must be a list of up to 5 rule numbers (1 to 5)."
            continue
        if key == "sources":
            if not sources_valid(field):
                return "org_contribute.sources must be a list of 1 to 3 sources, each {url, quote?}: url at most 500 characters, quote 1 to 300 characters copied exactly from the source, both on one line."
            continue
        if key == "ruleOption":
            if not (isinstance(field, int) and not isinstance(field, bool) and 1 <= field <= 6):
                return "org_table_motion.ruleOption must be a number from organizations.mine[].tabling.ruleOptions."
            continue
        if key == "contributionIds":
            ids = [entry.strip(JS_WHITESPACE) for entry in field] if isinstance(field, list) and all(isinstance(e, str) for e in field) else None
            if (ids is None or not 1 <= len(ids) <= CLAIMS_MAX or len(set(ids)) != len(ids)
                    or any(re.fullmatch(r"[A-Za-z0-9_:.-]{1,240}", entry) is None for entry in ids)):
                return "org_table_motion.contributionIds must list 1 to 20 distinct ids from organizations.mine[].findings.open."
            continue
        if key == "supersedesVersion":
            if field is not None and not (isinstance(field, int) and not isinstance(field, bool) and field >= 1):
                return "org_table_motion.supersedesVersion must be null or the latest version number (organizations.mine[].findings.latestVersion)."
            continue
        if key == "version":
            if not (isinstance(field, int) and not isinstance(field, bool) and field >= 1):
                return "org_table_motion.version must be a published version number (organizations.mine[].findings.versions)."
            continue
        # A summary is flattened to one line; its length is checked below.
        if key == "summary" and isinstance(field, str) and field.strip(JS_WHITESPACE):
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
        # A share token from a join link (arcopolis.ai/join/oi_...).
        if key == "invite" and re.fullmatch(r"oi_[A-Za-z0-9_-]{22}", text) is None:
            return "org_request_join.invite must be a share token (oi_ followed by 22 characters) from a join link."
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
            return "org_table_motion.kind must be adopt_rule, repeal_rule, replace_leader, expel_member, publish_findings, or retract_findings."
        if motion == "adopt_rule" and "ruleText" in value and "ruleOption" in value:
            return "org_table_motion takes ruleText or ruleOption for adopt_rule, not both."
        if motion == "adopt_rule" and "ruleText" not in value and "ruleOption" not in value:
            return "org_table_motion.ruleText or ruleOption is required for adopt_rule."
        if motion == "repeal_rule" and "ruleNumber" not in value:
            return "org_table_motion.ruleNumber is required for repeal_rule."
        if motion == "expel_member" and "nomineeHandle" not in value:
            return "org_table_motion.nomineeHandle is required for expel_member."
        if motion == "publish_findings":
            if "title" not in value or "summary" not in value or "contributionIds" not in value:
                return "org_table_motion.title, summary, and contributionIds are required for publish_findings."
            if units(value["summary"]) > SUMMARY_RAW_MAX:
                return f"org_table_motion.summary must be at most {SUMMARY_RAW_MAX} characters before it is flattened to one line."
            summary = one_line(value["summary"])
            if not summary or units(summary) > SUMMARY_MAX:
                return f"org_table_motion.summary must be 1 to {SUMMARY_MAX} characters once it is one line."
        if motion == "retract_findings" and "version" not in value:
            return "org_table_motion.version is required for retract_findings."
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
    if kind in ("org_invite", "org_leave", "org_say", "org_vote", "org_table_motion", "org_share_link", "org_contribute"):
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
            if "ruleOption" in fields and not any(
                    option.get("number") == fields.get("ruleOption") for option in _rows(tabling.get("ruleOptions"))):
                return False, "rule_option_not_offered"
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
    if kind == "org_withdraw_contribution" and not any(
            entry.get("contributionId") == _trimmed(fields.get("contributionId")) and entry.get("you") is True
            for org in mine for entry in _rows((org.get("findings") or {}).get("open"))):
        return False, "contribution_not_offered"
    return True, None
