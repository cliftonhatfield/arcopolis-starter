"""Offline peer chess contract regressions."""
import copy
import unittest
from chess import is_peer_chess_action, peer_chess_action_error, peer_chess_menu_error


def heartbeat():
    return {"menu": {"actions": ["chess_move", "chess_challenge"], "chessActions": ["chess_move", "chess_challenge"],
                     "budget": {"remaining": 0}, "chessBudget": {"remaining": 3, "worldRemaining": 30}},
            "chess": {"available": True, "turns": [{"gameId": "vchess_first", "yourTurn": True, "legalMoves": [{"uci": "e2e4"}]}]},
            "body": {"chess": [{"gameId": "resident_game", "yourTurn": True, "legalMoves": [{"uci": "a2a3"}]}]}}


class ChessTests(unittest.TestCase):
    def test_shapes_preserve_body(self):
        bodies = {"chess_challenge": {"handle": "@Cairn", "paceHours": 48},
                  "chess_respond": {"challengeId": "vchallenge_one", "reply": "accept"},
                  "chess_draw": {"gameId": "vchess_first", "reply": "offer"},
                  "chess_resign": {"gameId": "vchess_first"}, "chess_rematch": {"gameId": "vchess_first"}}
        original = copy.deepcopy(bodies)
        for kind, fields in bodies.items():
            self.assertIsNone(peer_chess_action_error(kind, fields))
        self.assertEqual(original, bodies)
        self.assertIsNone(peer_chess_action_error("chess_challenge", {"paceHours": 24}))

    def test_invalid_shapes(self):
        for kind, fields in [("chess_challenge", {"paceHours": "48"}), ("chess_challenge", {"paceHours": True}),
                             ("chess_challenge", {"paceHours": 48, "handle": "cairn", "agentId": "visitor_cairn"}),
                             ("chess_respond", {"challengeId": "vchallenge_one", "reply": "offer"}),
                             ("chess_draw", {"gameId": "vchess_first", "reply": "cancel"}), ("chess_resign", {})]:
            self.assertIsNotNone(peer_chess_action_error(kind, fields))

    def test_separate_budget_and_peer_turns(self):
        move = {"gameId": "vchess_first", "uci": "e2e4"}
        self.assertIsNone(peer_chess_menu_error(heartbeat(), "chess_move", move))
        self.assertEqual(peer_chess_menu_error(heartbeat(), "chess_move", {**move, "uci": "a2a3"})["code"], "INVALID_ACTION")
        for field in ("remaining", "worldRemaining"):
            data = heartbeat()
            data["menu"]["budget"]["remaining"] = 100
            data["menu"]["chessBudget"][field] = 0
            self.assertEqual(peer_chess_menu_error(data, "chess_move", move)["code"], "ACTION_BUDGET_EMPTY")
        data = heartbeat()
        data["chess"]["available"] = False
        self.assertEqual(peer_chess_menu_error(data, "chess_move", move)["code"], "ACTION_CLOSED")

    def test_server_normalization_preserves_request(self):
        move = {"gameId": " vchess_first ", "uci": " E2E4 "}
        original = copy.deepcopy(move)
        self.assertIsNone(peer_chess_menu_error(heartbeat(), "chess_move", move))
        self.assertEqual(move, original)

    def test_resident_chess_stays_with_existing_validator(self):
        self.assertFalse(is_peer_chess_action("chess_move", {"gameId": "resident_game"}))
        self.assertIsNone(peer_chess_menu_error({}, "chess_move", {"gameId": "resident_game", "uci": "e2e4"}))


if __name__ == "__main__":
    unittest.main()
