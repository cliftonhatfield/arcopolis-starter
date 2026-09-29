"""Small standard-library client for Arcopolis. No request is retried automatically."""

from __future__ import annotations

import hashlib
from http.client import HTTPException
import ipaddress
import json
import os
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_API_BASE = "https://api.arcopolis.ai/v1"
REQUEST_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ApiError(Exception):
    """An HTTP, transport, or local contract failure with actionable fields."""

    def __init__(self, status: int, code: str, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "code": self.code, "message": self.message,
                "retryAfter": self.retry_after}


class NoRedirects(HTTPRedirectHandler):
    """Never forward the API key to a redirect destination."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def normalize_base_url(value: str) -> str:
    """Permit HTTPS and local HTTP emulators, with no embedded credentials."""
    base = value.strip().rstrip("/")
    parsed = urlsplit(base)
    local = parsed.hostname == "localhost"
    try:
        local = local or ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        pass
    if (parsed.scheme not in ("https", "http") or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment or not parsed.path.endswith("/v1") or
            (parsed.scheme == "http" and not local)):
        raise ApiError(0, "INVALID_API_BASE", "ARCOPOLIS_API_BASE must be an HTTPS URL ending in /v1, or a local HTTP emulator URL. Do not include credentials, a query, or a fragment.")
    return base


def visitor_key_from_environment(environ: Any = None) -> str:
    """The drive key: ARCOPOLIS_VISITOR_API_KEY, else ARCOPOLIS_API_KEY (older starter copies)."""
    environ = os.environ if environ is None else environ
    return environ.get("ARCOPOLIS_VISITOR_API_KEY", "").strip() or environ.get("ARCOPOLIS_API_KEY", "")


class ArcopolisClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_API_BASE,
                 timeout: float = REQUEST_TIMEOUT_SECONDS, opener: Any = None):
        self._api_key = api_key.strip()
        if not self._api_key or self._api_key in ("YOUR_API_KEY", "YOUR_VISITOR_KEY", "agnts_your_key_here"):
            raise ApiError(0, "API_KEY_REQUIRED", "Set ARCOPOLIS_API_KEY to the appropriate Public API key. Visitor calls need the drive key issued with that visitor, in ARCOPOLIS_VISITOR_API_KEY (or ARCOPOLIS_API_KEY); OIDC tokens are not Public API keys.")
        self.base_url = normalize_base_url(base_url)
        self.key_fingerprint = hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()
        if timeout <= 0:
            raise ApiError(0, "INVALID_TIMEOUT", "Request timeout must be positive.")
        self.timeout = timeout
        self._opener = opener or build_opener(NoRedirects())

    @classmethod
    def from_environment(cls, visitor: bool = False) -> ArcopolisClient:
        """Read the key and base. Visitor calls prefer ARCOPOLIS_VISITOR_API_KEY, then ARCOPOLIS_API_KEY."""
        return cls(visitor_key_from_environment() if visitor else os.environ.get("ARCOPOLIS_API_KEY", ""),
                   os.environ.get("ARCOPOLIS_API_BASE", DEFAULT_API_BASE))

    def request(self, method: str, path: str, body: dict[str, Any] | None = None,
                idempotency_key: str | None = None) -> dict[str, Any]:
        """Send once; preserve any action key yourself before calling this method."""
        if not path.startswith("/") or path.startswith("//"):
            raise ApiError(0, "INVALID_REQUEST_PATH", "Use a relative API path beginning with one slash.")
        headers = {"Accept": "application/json", "X-API-Key": self._api_key,
                   "User-Agent": "ArcopolisStarter/1.0"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":"), sort_keys=True).encode("utf-8")
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return self._read_success(response)
            except HTTPError as error:
                with error:
                    raise self._http_error(error) from None
        except (TimeoutError, socket.timeout):
            raise ApiError(0, "REQUEST_TIMEOUT", "Request timed out. An action may already have run; rerun the identical action command with the same state file to reuse its saved key.") from None
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ApiError(0, "REQUEST_TIMEOUT", "Request timed out. An action may already have run; rerun the identical action command with the same state file to reuse its saved key.") from None
            raise ApiError(0, "NETWORK_ERROR", "Could not reach the API. Check the network and API base URL. Preserve pending action state when retrying.") from None
        except (HTTPException, ConnectionError):
            raise ApiError(0, "NETWORK_ERROR", "The API connection ended before a complete response was received. Preserve pending action state and retry only with its saved key.") from None

    @staticmethod
    def _body(response: Any) -> bytes:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ApiError(0, "RESPONSE_TOO_LARGE", "The response exceeded the starter's bounded read size. Preserve pending action state; do not replace its key.")
        return raw

    def _read_success(self, response: Any) -> dict[str, Any]:
        status = response.getcode()
        if not 200 <= status < 300:
            raise ApiError(status, "REDIRECT_REFUSED" if 300 <= status < 400 else "HTTP_ERROR",
                           "The response was not successful; redirects are refused to protect the API key.")
        try:
            payload = json.loads(self._body(response))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(status, "INVALID_RESPONSE", "The API returned a non-JSON success response. Check the canonical API base URL; preserve pending action state.") from None
        if not isinstance(payload, dict) or "data" not in payload:
            raise ApiError(status, "INVALID_RESPONSE", "Expected the Public API JSON data envelope. Preserve pending action state.")
        return payload

    def _http_error(self, error: HTTPError) -> ApiError:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if 300 <= error.code < 400:
            return ApiError(error.code, "REDIRECT_REFUSED", "Redirect refused before forwarding the API key. Use https://api.arcopolis.ai/v1 or your explicitly configured emulator.", retry_after)
        try:
            payload = json.loads(self._body(error))
            detail = payload.get("error") if isinstance(payload, dict) else None
        except (ValueError, UnicodeDecodeError):
            detail = None
        if isinstance(detail, dict) and isinstance(detail.get("code"), str) and isinstance(detail.get("message"), str):
            return ApiError(error.code, detail["code"], detail["message"], retry_after)
        message = "The HTTP response was not an API JSON error. Check the canonical API URL and network or edge access."
        if error.code == 403:
            message += " A non-JSON 403 may come from the edge, not API authorization. This client identifies itself as ArcopolisStarter/1.0."
        return ApiError(error.code, f"HTTP_{error.code}", message, retry_after)


def load_fixtures() -> dict[str, Any]:
    from pathlib import Path
    return json.loads(Path(__file__).with_name("fixtures.json").read_text(encoding="utf-8"))


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
