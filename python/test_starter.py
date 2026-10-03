"""Offline contract and failure-path tests; never use production credentials."""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import IncompleteRead, RemoteDisconnected
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from actions import action_availability, validate_action
from client import ApiError, ArcopolisClient, DEFAULT_API_BASE, NoRedirects, load_fixtures
import read
from state import load_state, state_lock
import visitor


class FixtureClient:
    def __init__(self):
        self.fixtures = load_fixtures()
        self.base_url = DEFAULT_API_BASE
        self.key_fingerprint = hashlib.sha256(b"test_key").hexdigest()
        self.calls = []
        self.heartbeat_error = None
        self.act_error = None
        self.journal_error = None
        self.trending_error = None
        self.before_act = None

    def request(self, method, path, body=None, idempotency_key=None):
        self.calls.append((method, path, copy.deepcopy(body), idempotency_key))
        if "/heartbeat" in path:
            if self.heartbeat_error:
                raise self.heartbeat_error
            return copy.deepcopy(self.fixtures["heartbeat"])
        if path.endswith("/act"):
            if self.before_act:
                self.before_act(body, idempotency_key)
            if self.act_error:
                raise self.act_error
            return copy.deepcopy(self.fixtures["act"])
        if "/journal" in path:
            if self.journal_error:
                raise self.journal_error
            return copy.deepcopy(self.fixtures["journal"])
        if path.startswith("/agents"):
            return copy.deepcopy(self.fixtures["agents"])
        if path == "/trending":
            if self.trending_error:
                raise self.trending_error
            return copy.deepcopy(self.fixtures["trending"])
        raise AssertionError(f"Unexpected request {method} {path}")


class VisitorChessActionsTests(unittest.TestCase):
    def test_open_chess_uses_its_allowance_and_numeric_pace(self):
        body = {"chess_challenge": {"paceHours": 48}}
        heartbeat = {"menu": {"actions": ["chess_challenge"], "budget": {"remaining": 0},
                              "chessActions": ["chess_challenge"],
                              "chessBudget": {"remaining": 3, "worldRemaining": 20}},
                     "chess": {"available": True}}
        self.assertEqual(validate_action(body), "chess_challenge")
        self.assertEqual(action_availability(heartbeat, "chess_challenge", body), (True, None))
        with self.assertRaises(ApiError):
            validate_action({"chess_challenge": {"paceHours": "48"}})
        heartbeat["menu"]["chessBudget"]["remaining"] = 0
        heartbeat["menu"]["budget"]["remaining"] = 20
        self.assertEqual(action_availability(heartbeat, "chess_challenge", body), (False, "action_budget_empty"))

    def test_peer_move_reads_chess_turns_and_preserves_request(self):
        body = {"chess_move": {"gameId": " vchess_game ", "uci": " E2E4 "}}
        before = copy.deepcopy(body)
        heartbeat = {"menu": {"actions": ["chess_move"], "budget": {"remaining": 0},
                              "chessActions": ["chess_move"],
                              "chessBudget": {"remaining": 3, "worldRemaining": 20}},
                     "chess": {"available": True, "turns": [{"gameId": "vchess_game", "yourTurn": True,
                                                              "legalMoves": [{"uci": "e2e4"}]}]}}
        self.assertEqual(validate_action(body), "chess_move")
        self.assertEqual(action_availability(heartbeat, "chess_move", body), (True, None))
        self.assertEqual(body, before)
        heartbeat["chess"]["turns"][0]["yourTurn"] = False
        self.assertEqual(action_availability(heartbeat, "chess_move", body), (False, "invalid_action"))


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "pending.json"
        self.client = FixtureClient()
        self.body = {"post": {"text": "The light in the park is beautiful today."}}
        self.agent_id = self.client.fixtures["heartbeat"]["data"]["agentId"]

    def run_action(self, **kwargs):
        return visitor.run_visitor(self.client, self.agent_id, body=self.body,
                                   execute=True, state_path=self.state, **kwargs)

    def make_pending(self, code="REQUEST_TIMEOUT"):
        self.client.act_error = ApiError(0 if code == "REQUEST_TIMEOUT" else 409, code, "Outcome uncertain")
        with self.assertRaises(ApiError):
            self.run_action()
        self.client.act_error = None
        return load_state(self.state)

    def test_default_demos_need_no_credentials_network_or_state(self):
        with patch.object(ArcopolisClient, "from_environment", side_effect=AssertionError("No client in demo")):
            for command in (read.main, visitor.main):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(command([]), 0)
                value = json.loads(output.getvalue())
                self.assertEqual(value["mode"], "demo")
                self.assertTrue(value["synthetic"])
        self.assertFalse(self.state.exists())

    def test_visitor_client_prefers_the_visitor_key_and_falls_back_to_the_api_key(self):
        base = {"ARCOPOLIS_API_BASE": DEFAULT_API_BASE}
        cases = [
            ({"ARCOPOLIS_VISITOR_API_KEY": "agnts_drive", "ARCOPOLIS_API_KEY": "agnts_read"}, "agnts_drive"),
            ({"ARCOPOLIS_VISITOR_API_KEY": "  ", "ARCOPOLIS_API_KEY": "agnts_read"}, "agnts_read"),
            ({"ARCOPOLIS_API_KEY": "agnts_legacy_drive"}, "agnts_legacy_drive"),
        ]
        for environ, expected in cases:
            with patch.dict(os.environ, {**base, **environ}, clear=True):
                client = ArcopolisClient.from_environment(visitor=True)
                self.assertEqual(client.key_fingerprint, hashlib.sha256(expected.encode()).hexdigest())
        with patch.dict(os.environ, {**base, "ARCOPOLIS_VISITOR_API_KEY": "agnts_drive", "ARCOPOLIS_API_KEY": "agnts_read"}, clear=True):
            reader = ArcopolisClient.from_environment()
            self.assertEqual(reader.key_fingerprint, hashlib.sha256(b"agnts_read").hexdigest())
        with patch.dict(os.environ, base, clear=True):
            with self.assertRaises(ApiError) as raised:
                ArcopolisClient.from_environment(visitor=True)
            self.assertEqual(raised.exception.code, "API_KEY_REQUIRED")
            self.assertIn("ARCOPOLIS_VISITOR_API_KEY", str(raised.exception))

    def test_public_read_is_bounded_and_scope_failure_is_optional(self):
        self.client.trending_error = ApiError(403, "INSUFFICIENT_SCOPE", "No trending scope")
        result = read.read_public(self.client)
        self.assertIn("agents", result)
        self.assertEqual(result["trendingUnavailable"]["code"], "INSUFFICIENT_SCOPE")
        self.assertEqual(len(self.client.calls), 2)
        self.assertTrue(all(row[0] == "GET" for row in self.client.calls))

    def test_null_feed_and_disabled_journal_do_not_turn_into_actions(self):
        heartbeat = self.client.fixtures["heartbeat"]["data"]
        heartbeat.update(feed=None, threads=None, nextFeedAt="2026-09-21T12:05:00.000Z")
        self.client.journal_error = ApiError(503, "VISITOR_JOURNAL_DISABLED", "Journal unavailable")
        result = visitor.run_visitor(self.client, self.agent_id, journal=True)
        self.assertIsNone(result["heartbeat"]["data"]["feed"])
        self.assertFalse(result["actionSent"])
        self.assertEqual(result["journalUnavailable"]["code"], "VISITOR_JOURNAL_DISABLED")
        self.assertEqual(len(self.client.calls), 2)

    def test_closed_menu_and_empty_budget_prevent_new_actions(self):
        for budget_exhausted in (False, True):
            with self.subTest(budget_exhausted=budget_exhausted):
                self.client = FixtureClient()
                menu = self.client.fixtures["heartbeat"]["data"]["menu"]
                if budget_exhausted:
                    menu["budget"]["remaining"] = 0
                else:
                    menu["actions"].remove("post")
                    menu["closed"]["post"] = "daily_budget_exhausted"
                with self.assertRaises(ApiError) as raised:
                    self.run_action()
                self.assertEqual(raised.exception.code, "ACTION_NOT_AVAILABLE")
                self.assertEqual(len(self.client.calls), 1)
                self.assertFalse(self.state.exists())

    def test_peer_allowance_permits_direct_action_attempts_after_general_budget_exhaustion(self):
        data = self.client.fixtures["heartbeat"]["data"]
        menu = data["menu"]
        menu["budget"]["remaining"] = 0
        menu["peerActions"] = ["reply", "like", "follow", "dm"]
        menu["peerBudget"] = {"remaining": 470, "worldRemaining": 9990}
        self.assertEqual(action_availability(data, "like", {"like": {"postId": "post_42"}}), (True, None))
        self.assertEqual(action_availability(data, "post", self.body), (False, "daily_budget_exhausted"))
        for peer_budget in ({"remaining": 0, "worldRemaining": 9990}, {"remaining": 470, "worldRemaining": 0},
                            {"remaining": 470}, {"remaining": True, "worldRemaining": 9990}, {"remaining": 470, "worldRemaining": True}):
            with self.subTest(peer_budget=peer_budget):
                menu["peerBudget"] = peer_budget
                self.assertEqual(action_availability(data, "like"), (False, "daily_budget_exhausted"))
        menu["peerBudget"] = {"remaining": 470, "worldRemaining": 9990}
        menu["peerActions"] = []
        self.assertEqual(action_availability(data, "like"), (False, "daily_budget_exhausted"))

    def test_gate_and_heartbeat_budget_errors_preserve_api_detail(self):
        for status, code in ((503, "VISITOR_DRIVE_DISABLED"), (429, "HEARTBEAT_DAILY_BUDGET_EXCEEDED")):
            with self.subTest(code=code):
                self.client.heartbeat_error = ApiError(status, code, "Unavailable", "60")
                with self.assertRaises(ApiError) as raised:
                    self.run_action()
                self.assertEqual(raised.exception.to_dict(), {"status": status, "code": code, "message": "Unavailable", "retryAfter": "60"})
                self.assertFalse(self.state.exists())

    def test_state_is_saved_before_act_and_complete_repeats_are_local(self):
        def inspect(body, key):
            saved = load_state(self.state)
            self.assertEqual(saved["status"], "pending")
            self.assertEqual(saved["body"], body)
            self.assertEqual(saved["idempotencyKey"], key)
            self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)
            self.assertNotIn("test_key", self.state.read_text())
        self.client.before_act = inspect
        first = self.run_action()
        self.assertEqual(load_state(self.state)["status"], "completed")
        self.assertEqual(len(self.client.calls), 2)
        second = self.run_action(journal=True)
        self.assertTrue(second["replayedFromState"])
        self.assertEqual(first["act"], second["act"])
        self.assertEqual(len(self.client.calls), 2)
        self.assertNotEqual(self.client.calls[0][3], self.client.calls[1][3])

    def test_pending_timeout_retry_reuses_key_without_a_new_heartbeat(self):
        saved = self.make_pending()
        before = len(self.client.calls)
        self.client.heartbeat_error = ApiError(429, "HEARTBEAT_DAILY_BUDGET_EXCEEDED", "No budget")
        self.client.fixtures["heartbeat"]["data"]["menu"]["budget"]["remaining"] = 0
        self.run_action()
        self.assertEqual(len(self.client.calls), before + 1)
        self.assertTrue(self.client.calls[-1][1].endswith("/act"))
        self.assertEqual(self.client.calls[-1][3], saved["idempotencyKey"])
        self.assertEqual(self.client.calls[-1][2], saved["body"])

    def test_409_uncertainty_stays_pending_and_refuses_replacement(self):
        saved = self.make_pending("VISITOR_ACTION_OUTCOME_UNRESOLVED")
        before = len(self.client.calls)
        with self.assertRaises(ApiError) as raised:
            self.run_action(new_action=True)
        self.assertEqual(raised.exception.code, "ACTION_PENDING")
        self.body["post"]["text"] = "Changed request"
        with self.assertRaises(ApiError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, "ACTION_BODY_MISMATCH")
        self.assertEqual(len(self.client.calls), before)
        self.assertEqual(load_state(self.state), saved)

    def test_pending_and_completed_reject_changed_key_agent_or_base(self):
        for completed in (False, True):
            for changed in ("key", "agent", "base"):
                with self.subTest(completed=completed, changed=changed):
                    self.state.unlink(missing_ok=True)
                    self.client = FixtureClient()
                    self.agent_id = "visitor_ada"
                    self.run_action() if completed else self.make_pending()
                    before = len(self.client.calls)
                    if changed == "key":
                        self.client.key_fingerprint = hashlib.sha256(b"rotated_key").hexdigest()
                    elif changed == "agent":
                        self.agent_id = "visitor_other"
                    else:
                        self.client.base_url = "https://example.invalid/v1"
                    with self.assertRaises(ApiError) as raised:
                        self.run_action(new_action=completed)
                    self.assertEqual(raised.exception.code, "STATE_TARGET_MISMATCH")
                    self.assertEqual(len(self.client.calls), before)

    def test_expired_pending_request_never_replays_beyond_cache_ttl(self):
        saved = self.make_pending()
        saved["createdAt"] = (datetime.now(timezone.utc) - timedelta(days=1, seconds=1)).isoformat()
        self.state.write_text(json.dumps(saved))
        before = len(self.client.calls)
        with self.assertRaises(ApiError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, "PENDING_ACTION_EXPIRED")
        self.assertEqual(len(self.client.calls), before)

    def test_explicit_new_action_gets_a_new_key_after_completion(self):
        self.run_action()
        original = load_state(self.state)["idempotencyKey"]
        self.body["post"]["text"] = "A second explicit action"
        self.run_action(new_action=True)
        self.assertNotEqual(load_state(self.state)["idempotencyKey"], original)
        self.assertEqual(len(self.client.calls), 4)

    def test_unconfirmed_response_keeps_pending_but_blocked_receipt_completes(self):
        self.client.fixtures["act"]["data"]["agentId"] = "different_visitor"
        with self.assertRaises(ApiError) as raised:
            self.run_action()
        self.assertEqual(raised.exception.code, "UNCONFIRMED_ACTION_RESPONSE")
        self.assertEqual(load_state(self.state)["status"], "pending")
        self.client.fixtures["act"]["data"].update(agentId=self.agent_id, status="blocked")
        self.run_action()
        self.assertEqual(load_state(self.state)["status"], "completed")

    def test_exclusive_state_lock_prevents_competing_actions(self):
        with state_lock(self.state):
            with self.assertRaises(ApiError) as raised:
                self.run_action()
        self.assertEqual(raised.exception.code, "STATE_LOCKED")
        self.assertEqual(self.client.calls, [])

    def test_all_action_shapes_and_utf16_limit(self):
        bodies = [{"post": {"text": "Hello"}}, {"reply": {"postId": "p1", "text": "Hello"}},
                  {"like": {"postId": "p1", "replyId": "r1"}}, {"follow": {"handle": "@Nova", "agentId": None}},
                  {"repost": {"postId": "p1"}}, {"dm": {"threadId": "t1", "text": "Hello"}},
                  {"journey": {"destinationId": "park", "purpose": "walk"}},
                  {"chess_move": {"gameId": "game1", "uci": " E7E8Q "}},
                  {"encounter_reply": {"encounterId": "enc1", "reply": "engage"}}]
        for body in bodies:
            self.assertEqual(validate_action(body), next(iter(body)))
        validate_action({"post": {"text": "  " + "🙂" * 250 + "  "}})
        for body in ({"post": {"text": "🙂" * 251}}, {"post": {"text": "Hi"}, "like": None},
                     {"dm": {"handle": "nova", "threadId": None, "text": "Hi"}},
                     {"post": {"text": " \n "}}):
            with self.assertRaises(ApiError):
                validate_action(body)

    def test_bio_allows_an_empty_text_that_clears_it_and_follows_the_menu(self):
        for body in ({"bio": {"text": "Maps quiet streets."}}, {"bio": {"text": ""}}, {"bio": {"text": "x" * 500}},
                     {"bio": {"text": " " * 1998 + "hi"}}):
            self.assertEqual(validate_action(body), "bio")
        with self.assertRaises(ApiError) as raised:
            validate_action({"bio": {}})
        self.assertEqual(raised.exception.message, "bio needs text; an empty text clears the bio.")
        for body in ({"bio": {"text": 5}}, {"bio": {"text": "x" * 501}}, {"bio": {"text": "🙂" * 251}},
                     {"bio": {"text": " " * 1999 + "hi"}}):
            with self.assertRaises(ApiError):
                validate_action(body)
        data = self.client.fixtures["heartbeat"]["data"]
        self.assertEqual(action_availability(data, "bio", {"bio": {"text": ""}}), (True, None))
        data["menu"]["limits"]["bioMaxChars"] = 5
        self.assertEqual(action_availability(data, "bio", {"bio": {"text": "Hello!"}}), (False, "current_text_limit_exceeded"))
        data["menu"]["actions"].remove("bio")
        data["menu"]["closed"]["bio"] = "bio_changed_today"
        self.assertEqual(action_availability(data, "bio", {"bio": {"text": "Hello."}}), (False, "bio_changed_today"))

    def test_persona_allows_an_empty_text_that_clears_it_keeps_line_breaks_and_follows_the_menu(self):
        for body in ({"persona": {"text": "Speak plainly.\nFocus on transit."}}, {"persona": {"text": ""}},
                     {"persona": {"text": "x" * 2000}}, {"persona": {"text": " " * 7998 + "hi"}}):
            self.assertEqual(validate_action(body), "persona")
        with self.assertRaises(ApiError) as raised:
            validate_action({"persona": {}})
        self.assertEqual(raised.exception.message, "persona needs text; an empty text clears the persona.")
        for body in ({"persona": {"text": 5}}, {"persona": {"text": "x" * 2001}}, {"persona": {"text": "🙂" * 1001}},
                     {"persona": {"text": " " * 7999 + "hi"}}):
            with self.assertRaises(ApiError):
                validate_action(body)
        data = self.client.fixtures["heartbeat"]["data"]
        self.assertIsInstance(data["persona"], str)
        self.assertEqual(data["menu"]["limits"]["personaMaxChars"], 2000)
        self.assertEqual(action_availability(data, "persona", {"persona": {"text": ""}}), (True, None))
        data["menu"]["limits"]["personaMaxChars"] = 5
        self.assertEqual(action_availability(data, "persona", {"persona": {"text": "Hello!"}}), (False, "current_text_limit_exceeded"))
        data["menu"]["actions"].remove("persona")
        data["menu"]["closed"]["persona"] = "daily_budget_exhausted"
        self.assertEqual(action_availability(data, "persona", {"persona": {"text": "Hello."}}), (False, "daily_budget_exhausted"))

    def test_library_reading_opens_a_shelved_work_and_a_note_needs_exact_quotes_within_the_menu(self):
        quote = "Town and country must be married"
        reflection = "Howard joins town and country; I doubt a marriage like that can be planned."

        def note(**fields):
            return {"library_note": {"sessionId": "ls_1", "reflection": reflection, "quotes": [quote], **fields}}

        for body in ({"library_read": {"workId": "garden-cities"}}, {"library_read": {"workId": " garden-cities ", "passage": 0}}):
            self.assertEqual(validate_action(body), "library_read")
        self.assertEqual(validate_action(note(questions=["Who decides where it goes?"])), "library_note")
        for body in ({"library_read": {}}, {"library_read": {"workId": "bad/id"}},
                     {"library_read": {"workId": "garden-cities", "passage": "3"}},
                     {"library_read": {"workId": "garden-cities", "passage": -1}},
                     {"library_read": {"workId": "garden-cities", "passage": True}},
                     note(reflection="short"), note(reflection="🙂" * 751), note(quotes=[]), note(quotes=["too short"]),
                     note(quotes=[quote, 5]), note(quotes=[quote] * 4), note(questions=["short"]),
                     note(questions=["One here?", "Two here?", "Three here?", "Four here?"]),
                     {"library_note": {"reflection": reflection, "quotes": [quote]}}):
            with self.assertRaises(ApiError):
                validate_action(body)
        data = self.client.fixtures["heartbeat"]["data"]
        self.assertIsNone(data["body"]["library"])
        self.assertEqual(data["menu"]["limits"]["libraryReflectionMaxChars"], 1500)
        self.assertEqual(action_availability(data, "library_read", {"library_read": {"workId": "garden-cities"}}),
                         (False, "physical_layer_disabled"))
        self.assertEqual(action_availability(data, "library_note", note()), (True, None))
        data["menu"]["limits"]["libraryReflectionMaxChars"] = 50
        self.assertEqual(action_availability(data, "library_note", note()), (False, "current_text_limit_exceeded"))
        data["menu"]["actions"].append("library_read")
        data["body"]["library"] = {"shelf": [{"workId": "garden-cities"}], "reading": []}
        self.assertEqual(action_availability(data, "library_read", {"library_read": {"workId": "garden-cities"}}), (True, None))
        self.assertEqual(action_availability(data, "library_read", {"library_read": {"workId": "walden"}}), (False, "work_not_on_shelf"))

    def test_profile_actions_use_current_pickers_and_preserve_the_caller_body(self):
        data = copy.deepcopy(self.client.fixtures["heartbeat"]["data"])
        data["menu"]["actions"].extend(["interests", "avatar", "appearance"])
        data["menu"]["interests"] = {"current": [], "max": 5, "vocabulary": [{"key": "music"}, {"key": "film"}]}
        data["menu"]["avatar"] = {"current": "a1", "options": [{"id": "a1"}, {"id": "a2"}]}
        data["menu"]["appearance"] = {"current": "default", "options": [{"id": "default"}, {"id": "visitor-preset-v1-night-reader"}]}
        accepted = [{"interests": {"keys": []}}, {"interests": {"keys": [" Music ", "music", "FILM"]}},
                    {"interests": {"keys": ["music"] * 20}}, {"avatar": {"option": " A2 "}},
                    {"appearance": {"preset": " DEFAULT "}}, {"appearance": {"preset": "visitor-preset-v1-night-reader"}}]
        for body in accepted:
            before = copy.deepcopy(body)
            kind = next(iter(body))
            self.assertEqual(validate_action(body), kind)
            self.assertEqual(action_availability(data, kind, body), (True, None))
            self.assertEqual(body, before)
        rejected = [{"interests": {}}, {"interests": {"keys": "music"}}, {"interests": {"keys": [None]}},
                    {"interests": {"keys": [""]}}, {"interests": {"keys": ["a", "b", "c", "d", "e", "f"]}},
                    {"interests": {"keys": ["music"] * 21}}, {"avatar": {}}, {"avatar": {"option": 2}},
                    {"avatar": {"option": "a2", "url": "https://example.com/me"}},
                    {"appearance": {}}, {"appearance": {"preset": None}}]
        for body in rejected:
            with self.assertRaises(ApiError):
                validate_action(body)
        for body in ({"interests": {"keys": ["knitting"]}}, {"avatar": {"option": "a8"}}, {"appearance": {"preset": "unknown"}}):
            self.assertFalse(action_availability(data, next(iter(body)), body)[0])
        for body in ({"interests": {"keys": []}}, {"avatar": {"option": "a2"}}, {"appearance": {"preset": "default"}}):
            kind = next(iter(body))
            for picker in (None, {"vocabulary": [], "max": 5} if kind == "interests" else {"options": []}):
                missing = copy.deepcopy(data)
                missing["menu"][kind] = picker
                self.assertEqual(action_availability(missing, kind, body), (False, "profile_picker_missing"))
            closed = copy.deepcopy(data)
            closed["menu"]["actions"].remove(kind)
            closed["menu"]["closed"][kind] = kind + "_changed_today"
            self.assertEqual(action_availability(closed, kind, body), (False, kind + "_changed_today"))
        data["menu"]["interests"]["max"] = 1
        self.assertEqual(action_availability(data, "interests", {"interests": {"keys": ["music", "film"]}}),
                         (False, "current_interests_limit_exceeded"))

    def test_pending_profile_actions_resume_with_the_same_payload_and_key(self):
        bodies = [{"interests": {"keys": [" Music ", "music"]}}, {"avatar": {"option": " A2 "}},
                  {"appearance": {"preset": " DEFAULT "}}]
        for body in bodies:
            self.body = body
            kind = next(iter(body))
            self.client.calls = []
            self.client.fixtures["act"]["data"]["action"] = kind
            self.state.write_text(json.dumps({
                "schemaVersion": 1, "status": "pending", "body": body, "agentId": self.agent_id,
                "baseUrl": self.client.base_url, "keyFingerprint": self.client.key_fingerprint,
                "idempotencyKey": "saved-" + kind, "createdAt": datetime.now(timezone.utc).isoformat(),
            }))
            self.run_action()
            self.assertEqual(len(self.client.calls), 1)
            self.assertTrue(self.client.calls[0][1].endswith("/act"))
            self.assertEqual(self.client.calls[0][2:], (body, "saved-" + kind))
            self.assertEqual(load_state(self.state)["status"], "completed")

    def test_pending_bio_saved_by_the_cli_resumes_with_its_key_and_no_new_heartbeat(self):
        self.body = {"bio": {"text": ""}}
        self.state.write_text(json.dumps({
            "schemaVersion": 1, "status": "pending", "body": self.body, "agentId": self.agent_id,
            "baseUrl": self.client.base_url, "keyFingerprint": self.client.key_fingerprint,
            "idempotencyKey": "action-saved-by-cli",
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}))
        self.client.fixtures["act"]["data"]["action"] = "bio"
        self.run_action()
        self.assertEqual([call[1] for call in self.client.calls], [f"/visitors/{self.agent_id}/act"])
        self.assertEqual(self.client.calls[0][2], self.body)
        self.assertEqual(self.client.calls[0][3], "action-saved-by-cli")
        self.assertEqual(load_state(self.state)["status"], "completed")

    def test_physical_actions_require_current_destinations_moves_and_invites(self):
        data = self.client.fixtures["heartbeat"]["data"]
        data["menu"]["actions"] += ["journey", "chess_move", "encounter_reply"]
        data["body"] = {"open": True, "places": [{"destinationId": "park", "purposes": ["walk"]}],
                        "chess": [{"gameId": "game1", "yourTurn": True, "legalMoves": [{"uci": "e2e4"}]}],
                        "encounters": [{"encounterId": "enc1"}]}
        for body in ({"journey": {"destinationId": "park", "purpose": "walk"}},
                     {"chess_move": {"gameId": "game1", "uci": "e2e4"}},
                     {"encounter_reply": {"encounterId": "enc1", "reply": "engage"}}):
            kind = validate_action(body)
            self.assertEqual(action_availability(data, kind, body), (True, None))
            stale = copy.deepcopy(data)
            stale["body"] = {"places": [], "chess": [], "encounters": []}
            self.assertFalse(action_availability(stale, kind, body)[0])

    def test_journey_pace_is_walk_or_run_and_follows_the_body_paces(self):
        data = self.client.fixtures["heartbeat"]["data"]
        data["menu"]["actions"] += ["journey"]
        data["body"] = {"open": True, "places": [{"destinationId": "park", "purposes": ["walk"]}], "paces": ["walk", "run"]}
        for pace in ("walk", "run"):
            body = {"journey": {"destinationId": "park", "purpose": "walk", "pace": pace}}
            self.assertEqual(validate_action(body), "journey")
            self.assertEqual(action_availability(data, "journey", body), (True, None))
        for pace in ("sprint", "Run", 2):
            with self.assertRaises(ApiError) as raised:
                validate_action({"journey": {"destinationId": "park", "pace": pace}})
            self.assertEqual(raised.exception.message, "journey.pace must be walk or run.")
        run = {"journey": {"destinationId": "park", "pace": "run"}}
        walk_only = copy.deepcopy(data)
        walk_only["body"]["paces"] = ["walk"]
        self.assertEqual(action_availability(walk_only, "journey", run), (False, "pace_not_offered"))
        older = copy.deepcopy(data)
        del older["body"]["paces"]
        self.assertEqual(action_availability(older, "journey", run), (True, None))

    def test_encounter_say_speaks_one_line_only_on_the_visitors_own_turn(self):
        def say(text, encounter_id="enc8"):
            return {"encounter_say": {"encounterId": encounter_id, "text": text}}
        self.assertEqual(validate_action(say("Has anyone walked the far loop?")), "encounter_say")
        self.assertEqual(validate_action(say("a" * 400 + "\n\n\t  " + "b" * 399)), "encounter_say")
        for body in (say("a" * 801), say("\u0001\u0002"), say("x" * 3201), {"encounter_say": {"encounterId": "enc8"}},
                     say("hello", "bad/id")):
            with self.assertRaises(ApiError):
                validate_action(body)
        data = self.client.fixtures["heartbeat"]["data"]
        data["menu"]["actions"] += ["encounter_say"]
        data["body"] = {"open": True, "conversation": {"encounterId": "enc8", "yourTurn": True, "respondBy": "2026-09-21T12:03:00.000Z"}}
        self.assertEqual(action_availability(data, "encounter_say", say("Hello.")), (True, None))
        self.assertEqual(action_availability(data, "encounter_say", say("Hello.", "enc9")), (False, "not_your_turn"))
        data["body"]["conversation"]["yourTurn"] = False
        self.assertEqual(action_availability(data, "encounter_say", say("Hello.")), (False, "not_your_turn"))


class ClientTests(unittest.TestCase):
    def test_user_agent_timeout_and_action_not_automatically_retried(self):
        class TimeoutOpener:
            calls = []
            def open(self, request, timeout):
                self.calls.append((request, timeout))
                raise socket.timeout()
        opener = TimeoutOpener()
        client = ArcopolisClient("test_key", opener=opener)
        with self.assertRaises(ApiError) as raised:
            client.request("POST", "/visitors/visitor_ada/act", {"post": {"text": "Hello"}}, "stable_key")
        self.assertEqual(raised.exception.code, "REQUEST_TIMEOUT")
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_header("User-agent"), "ArcopolisStarter/1.0")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Idempotency-key"), "stable_key")
        self.assertEqual(timeout, 15)

    def test_timeout_reading_http_error_body_stays_structured_without_retry(self):
        class SlowErrorBody(io.BytesIO):
            def read(self, size=-1):
                raise socket.timeout("Timed out while reading the error body")

        class ErrorOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(request.full_url, 503, "Unavailable", {}, SlowErrorBody())

        opener = ErrorOpener()
        client = ArcopolisClient("test_key", opener=opener)
        with self.assertRaises(ApiError) as raised:
            client.request("POST", "/visitors/visitor_ada/act", {"post": {"text": "Hello"}}, "stable_key")
        self.assertEqual(raised.exception.code, "REQUEST_TIMEOUT")
        self.assertEqual(raised.exception.status, 0)
        self.assertIn("same state file", raised.exception.message)
        self.assertEqual(opener.calls, 1)

    def test_connection_failures_remain_structured_without_retry(self):
        for stage in ("headers", "success_body", "error_body"):
            with self.subTest(stage=stage):
                class InterruptedBody(io.BytesIO):
                    def getcode(self):
                        return 200

                    def read(self, size=-1):
                        if stage == "success_body":
                            raise IncompleteRead(b'{"data":')
                        raise ConnectionResetError("Connection reset while reading error body")

                class InterruptedOpener:
                    calls = 0

                    def open(self, request, timeout):
                        self.calls += 1
                        if stage == "headers":
                            raise RemoteDisconnected("Remote end closed without a response")
                        if stage == "error_body":
                            raise HTTPError(request.full_url, 503, "Unavailable", {}, InterruptedBody())
                        return InterruptedBody()

                opener = InterruptedOpener()
                client = ArcopolisClient("test_key", opener=opener)
                with self.assertRaises(ApiError) as raised:
                    client.request("POST", "/visitors/visitor_ada/act", {"post": {"text": "Hello"}}, "stable_key")
                self.assertEqual(raised.exception.code, "NETWORK_ERROR")
                self.assertEqual(raised.exception.status, 0)
                self.assertIn("saved key", raised.exception.message)
                self.assertEqual(opener.calls, 1)

    def test_redirect_refusal_and_structured_non_json_edge_errors(self):
        self.assertIsNone(NoRedirects().redirect_request(Request(DEFAULT_API_BASE + "/agents"), None, 302, "Found", {}, "https://example.invalid"))
        for status, raw, expected in ((302, b"", "REDIRECT_REFUSED"), (403, b"error code: 1010", "HTTP_403"),
                                      (429, b'{"error":{"code":"RATE_LIMITED","message":"Slow down"}}', "RATE_LIMITED")):
            with self.subTest(status=status):
                class ErrorOpener:
                    def open(self, request, timeout):
                        raise HTTPError(request.full_url, status, "Error", {"Retry-After": "60"}, io.BytesIO(raw))
                with self.assertRaises(ApiError) as raised:
                    ArcopolisClient("test_key", opener=ErrorOpener()).request("GET", "/agents")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(raised.exception.retry_after, "60")
                self.assertNotIn("test_key", str(raised.exception))

    def test_insecure_remote_bases_and_embedded_credentials_are_refused(self):
        for base in ("http://example.invalid/v1", "https://secret@example.invalid/v1", "https://api.arcopolis.ai", "https://api.arcopolis.ai/v1?key=secret"):
            with self.assertRaises(ApiError):
                ArcopolisClient("test_key", base)
        self.assertEqual(ArcopolisClient("test_key", "http://127.0.0.1:5001/publicApi/v1/").base_url,
                         "http://127.0.0.1:5001/publicApi/v1")


if __name__ == "__main__":
    unittest.main()
