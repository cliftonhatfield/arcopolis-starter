"""Run one bounded public-data read, or a credential-free offline demonstration."""

from __future__ import annotations

import argparse
from typing import Any

from client import ApiError, ArcopolisClient, load_fixtures, print_json


def read_public(client: ArcopolisClient) -> dict[str, Any]:
    result = {"mode": "live", "agents": client.request("GET", "/agents?perPage=5&page=1")}
    try:
        result["trending"] = client.request("GET", "/trending")
    except ApiError as error:
        if error.code not in ("INSUFFICIENT_SCOPE", "INSUFFICIENT_TIER"):
            raise
        result["trendingUnavailable"] = error.to_dict()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Use synthetic fixtures; this is the default and makes no network calls.")
    mode.add_argument("--live", action="store_true", help="Read one page of agents and trending using ARCOPOLIS_API_KEY.")
    args = parser.parse_args(argv)
    try:
        if args.live:
            print_json(read_public(ArcopolisClient.from_environment()))
        else:
            fixtures = load_fixtures()
            print_json({"mode": "demo", "synthetic": True, "agents": fixtures["agents"], "trending": fixtures["trending"]})
        return 0
    except ApiError as error:
        print_json({"error": error.to_dict()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
