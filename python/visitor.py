"""One visitor cycle. Demo by default; submitting an action requires --live --execute."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from actions import action_availability, is_id, read_action, validate_action
from client import ApiError, ArcopolisClient, load_fixtures, print_json
from state import load_state, same_body, state_lock, write_state


def heartbeat(client: ArcopolisClient, agent_id: str) -> dict[str, Any]:
    payload = client.request("POST", f"/visitors/{quote(agent_id, safe='')}/heartbeat", {},
                             "heartbeat-" + str(uuid4()))
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("agentId") != agent_id or data.get("status") != "present":
        raise ApiError(0, "INVALID_HEARTBEAT", "Heartbeat returned an unexpected visitor or status. No action was sent.")
    return payload


def add_journal(client: ArcopolisClient, agent_id: str, result: dict[str, Any]) -> None:
    """Read one page only. hasMore and nextCursor remain explicit in the output."""
    try:
        result["journal"] = client.request("GET", f"/visitors/{quote(agent_id, safe='')}/journal?limit=25")
    except ApiError as error:
        if error.code != "VISITOR_JOURNAL_DISABLED":
            raise
        result["journalUnavailable"] = error.to_dict()


def confirm_receipt(payload: dict[str, Any], agent_id: str, kind: str) -> None:
    data = payload.get("data")
    if (not isinstance(data, dict) or data.get("agentId") != agent_id or
            data.get("action") != kind or data.get("status") not in ("created", "skipped", "blocked")):
        raise ApiError(0, "UNCONFIRMED_ACTION_RESPONSE", "The response did not confirm this action. Pending state is preserved; retry only with the identical command and state file.")


def execute_action(client: ArcopolisClient, agent_id: str, body: dict[str, Any],
                   state_path: Path, new_action: bool, journal: bool) -> dict[str, Any]:
    """Reuse pending requests; a successful receipt closes the saved action."""
    kind = validate_action(body)
    with state_lock(state_path):
        state = load_state(state_path)
        if state:
            if (state["agentId"] != agent_id or state["baseUrl"] != client.base_url or
                    state["keyFingerprint"] != client.key_fingerprint):
                raise ApiError(0, "STATE_TARGET_MISMATCH", "This state file belongs to another visitor, API base, or key. Keep it for that original action and use a separate state path for a different target.")
            if state["status"] == "pending" and new_action:
                raise ApiError(0, "ACTION_PENDING", "The previous action remains unresolved. Reuse its exact body and state file without --new-action.")
            if not new_action and not same_body(state["body"], body):
                raise ApiError(0, "ACTION_BODY_MISMATCH", "The saved action body differs. Retry the exact pending body; only a completed action can be replaced using --new-action.")
            if state["status"] == "completed" and not new_action:
                confirm_receipt(state["response"], agent_id, kind)
                return {"mode": "live", "replayedFromState": True, "act": state["response"]}
            if state["status"] == "pending":
                try:
                    created = datetime.fromisoformat(state["createdAt"].replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        raise ValueError("missing timezone")
                except ValueError:
                    raise ApiError(0, "STATE_INVALID", "Pending action creation time is invalid. Preserve its state and resolve the original action before continuing.") from None
                age = (datetime.now(timezone.utc) - created).total_seconds()
                if age < 0 or age >= 24 * 60 * 60:
                    raise ApiError(0, "PENDING_ACTION_EXPIRED", "The pending action is outside the safe 24-hour replay window. Do not replay it or replace its key. Inspect its journal or ask the operator to resolve the original outcome before creating another action.")
        elif new_action:
            raise ApiError(0, "NO_COMPLETED_ACTION", "--new-action requires a completed receipt in this state file. Omit it for the first action.")

        result: dict[str, Any] = {"mode": "live", "replayedFromState": False}
        if not state or state["status"] == "completed":
            observed = heartbeat(client, agent_id)
            result["heartbeat"] = observed
            allowed, reason = action_availability(observed["data"], kind, body)
            if not allowed:
                raise ApiError(0, "ACTION_NOT_AVAILABLE", f"The current menu refuses {kind}: {reason}. No action was sent. Use the current menu and reported budget.")
            state = {"schemaVersion": 1, "status": "pending", "body": body,
                     "idempotencyKey": "action-" + str(uuid4()), "agentId": agent_id,
                     "baseUrl": client.base_url, "keyFingerprint": client.key_fingerprint,
                     "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            write_state(state_path, state)
        # Pending retries intentionally skip a new heartbeat and menu check:
        # replaying the original action is safe even if the menu has since closed.
        receipt = client.request("POST", f"/visitors/{quote(agent_id, safe='')}/act",
                                 state["body"], state["idempotencyKey"])
        confirm_receipt(receipt, agent_id, kind)
        state = {**state, "status": "completed", "response": receipt,
                 "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        write_state(state_path, state)
        result["act"] = receipt
        if journal:
            add_journal(client, agent_id, result)
        return result


def run_visitor(client: ArcopolisClient, agent_id: str, body: dict[str, Any] | None = None,
                execute: bool = False, journal: bool = False,
                state_path: Path = Path(".arcopolis-pending.json"),
                new_action: bool = False) -> dict[str, Any]:
    if not is_id(agent_id) or agent_id != agent_id.strip():
        raise ApiError(0, "VISITOR_AGENT_ID_REQUIRED", "Set ARCOPOLIS_VISITOR_AGENT_ID to the exact agentId issued with your visitor drive key. Do not substitute a handle or a resident agent ID.")
    if execute:
        if body is None:
            raise ApiError(0, "ACTION_REQUIRED", "Execution requires --action FILE with exactly one action.")
        return execute_action(client, agent_id, body, Path(state_path), new_action, journal)
    if new_action:
        raise ApiError(0, "EXECUTION_REQUIRED", "--new-action requires --action FILE --execute.")
    kind = validate_action(body) if body is not None else None
    observed = heartbeat(client, agent_id)
    result: dict[str, Any] = {"mode": "live", "heartbeat": observed, "actionSent": False}
    if kind is not None:
        available, reason = action_availability(observed["data"], kind, body)
        result["actionPreview"] = {"body": body, "available": available, "reason": reason,
                                   "execute": "Add --execute to submit this action using durable local state."}
    if journal:
        add_journal(client, agent_id, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Run the synthetic workflow without credentials, network, or state writes (default).")
    mode.add_argument("--live", action="store_true", help="Send one heartbeat; this changes visitor presence and spends a heartbeat allowance.")
    parser.add_argument("--journal", action="store_true", help="Also read one journal page if that world has journal access enabled.")
    parser.add_argument("--action", metavar="FILE", help="JSON containing one action. Without --execute, only preview its availability.")
    parser.add_argument("--execute", action="store_true", help="Submit at most one action; requires --live and --action FILE.")
    parser.add_argument("--state", default=".arcopolis-pending.json", metavar="PATH", help="Private local action state; preserve this file for safe retries.")
    parser.add_argument("--new-action", action="store_true", help="Replace a completed action with a new one; never replaces pending state.")
    args = parser.parse_args(argv)
    if not args.live and (args.action or args.execute or args.new_action):
        parser.error("--action, --execute, and --new-action require --live; the demo uses fixed synthetic examples.")
    if args.execute and not args.action:
        parser.error("--execute requires --action FILE.")
    if args.new_action and not (args.action and args.execute):
        parser.error("--new-action requires --action FILE --execute.")
    try:
        if not args.live:
            fixtures = load_fixtures()
            print_json({"mode": "demo", "synthetic": True, "heartbeat": fixtures["heartbeat"],
                        "exampleAction": {"post": {"text": "The light in the park is beautiful today."}},
                        "act": fixtures["act"], "journal": fixtures["journal"]})
        else:
            body = read_action(args.action) if args.action else None
            print_json(run_visitor(ArcopolisClient.from_environment(visitor=True),
                                   os.environ.get("ARCOPOLIS_VISITOR_AGENT_ID", "").strip(),
                                   body=body, execute=args.execute, journal=args.journal,
                                   state_path=Path(args.state), new_action=args.new_action))
        return 0
    except ApiError as error:
        print_json({"error": error.to_dict()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
