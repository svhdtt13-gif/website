"""IS3B2 single-shot canary network boundary.
Exactly one production-capable canary transport exists in this codebase, and
it lives in this module. It performs a single HTTP POST to an allowlisted
loopback destination with an explicit timeout, no redirect following, a
single attempt only, no batching, no queueing, and no background work.

Any network I/O whose outcome cannot prove success is reported as ambiguous;
callers must record `unknown` and never transmit again for the same identity.
"""
from __future__ import annotations

import hashlib
import http.client
import urllib.error
import urllib.parse
import urllib.request
from typing import Final

CANARY_SEND_CONTRACT_VERSION: Final = "is3b2.v1"

CANARY_DESTINATION_ALLOWLIST: Final = frozenset(
    {
        "127.0.0.1",
        "localhost",
    }
)

_MAX_RESPONSE_BYTES: Final = 65536
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0


class CanaryTransportError(Exception):
    """Base error for the single-shot canary transport."""


class CanaryDestinationRefused(CanaryTransportError):
    """The destination is outside the reviewed allowlist, or reuse was attempted."""


class CanarySendAmbiguous(CanaryTransportError):
    """The bytes may have reached the remote; the caller must record unknown."""

    def __init__(self, status_class: str):
        super().__init__(status_class)
        self.status_class = status_class


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CanarySendAmbiguous("redirect_response")


def _allowlisted_destination(destination_url: str) -> str:
    if not isinstance(destination_url, str) or not destination_url:
        raise CanaryDestinationRefused("destination_invalid")
    try:
        parts = urllib.parse.urlsplit(destination_url)
    except ValueError:
        raise CanaryDestinationRefused("destination_invalid") from None
    if parts.scheme != "http":
        raise CanaryDestinationRefused("destination_scheme_invalid")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise CanaryDestinationRefused("destination_credentials_forbidden")
    if (parts.hostname or "").lower() not in CANARY_DESTINATION_ALLOWLIST:
        raise CanaryDestinationRefused("destination_not_allowlisted")
    if not parts.port:
        raise CanaryDestinationRefused("destination_port_required")
    if parts.query or parts.fragment:
        raise CanaryDestinationRefused("destination_query_forbidden")
    return destination_url


class SingleShotCanaryTransport:
    """One-shot HTTP POST to an allowlisted canary destination.

    The instance becomes consumed after the first transmission attempt,
    successful or not, so a second call can never produce a second mutation.
    """

    kind = "single_shot"

    def __init__(
        self,
        destination_url: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        credential_provider=None,
        contract_version: str = CANARY_SEND_CONTRACT_VERSION,
    ):
        self.destination_url = _allowlisted_destination(destination_url)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise CanaryDestinationRefused("timeout_invalid")
        self.timeout = float(timeout)
        self.credential_provider = credential_provider
        self.contract_version = contract_version
        self._consumed = False
        self.transmissions = 0

    def _credential(self):
        if self.credential_provider is None:
            return None
        value = self.credential_provider()
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise CanaryDestinationRefused("credential_invalid")
        return value

    def single_post(
        self,
        body_json: str,
        idempotency_key: str,
        pre_send_identity: str,
    ) -> tuple[str, str]:
        """Transmit once. Returns (status_class, response_fingerprint).

        Raises CanaryDestinationRefused before any byte leaves on misuse, and
        CanarySendAmbiguous whenever success cannot be proved.
        """
        if self._consumed:
            raise CanaryDestinationRefused("transport_already_consumed")
        if not isinstance(body_json, str) or not body_json:
            raise CanaryDestinationRefused("body_invalid")
        if not idempotency_key or not pre_send_identity:
            raise CanaryDestinationRefused("identity_invalid")
        self._consumed = True
        credential = self._credential()
        headers = {
            "Content-Type": "application/json",
            "X-Canary-Idempotency-Key": idempotency_key,
            "X-Pre-Send-Identity": pre_send_identity,
        }
        if credential is not None:
            headers["Authorization"] = credential
        opener = urllib.request.build_opener(_RefuseRedirect)
        request = urllib.request.Request(
            self.destination_url,
            data=body_json.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        self.transmissions += 1
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status = response.status
                try:
                    raw = response.read(_MAX_RESPONSE_BYTES + 1)
                except (OSError, http.client.HTTPException, ValueError):
                    raise CanarySendAmbiguous("response_read_failed") from None
        except CanarySendAmbiguous:
            raise
        except urllib.error.HTTPError:
            raise CanarySendAmbiguous("http_error_response") from None
        except TimeoutError:
            raise CanarySendAmbiguous("timeout") from None
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                raise CanarySendAmbiguous("timeout") from None
            raise CanarySendAmbiguous("connection_failed") from None
        except (ConnectionError, OSError):
            raise CanarySendAmbiguous("connection_failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CanarySendAmbiguous("response_too_large")
        if not 200 <= status < 300:
            raise CanarySendAmbiguous("http_error_response")
        status_class = f"http_{status}"
        fingerprint = hashlib.sha256(raw).hexdigest()
        return status_class, fingerprint
