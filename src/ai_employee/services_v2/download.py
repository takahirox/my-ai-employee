from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from ai_employee.domain.base import freeze_json
from ai_employee.domain.services_v2 import ArtifactStore, Cancellation
from ai_employee.domain.v2 import (
    ArtifactPutRequest,
    DecisionOutcome,
    DownloadRequest,
    DownloadResult,
    PolicyDecision,
    StableFailure,
    StableFailureCode,
)

from ._common import identifier, now


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO
    peer_ip: str


class DownloadTransport(Protocol):
    def __call__(
        self, url: str, peer_ip: str, connect_timeout: float, read_timeout: float
    ) -> TransportResponse: ...


Resolver = Callable[[str, int], Sequence[str]]


class _IntegrityError(Exception):
    pass


class _DownloadCancelled(Exception):
    pass


class RestrictedDownloadClient:
    """HTTPS-only downloader with per-hop policy and public-address validation."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        *,
        enabled: bool = False,
        allowed_domains: Sequence[str] = (),
        allowed_ports: Sequence[int] = (443,),
        resolver: Resolver | None = None,
        transport: DownloadTransport | None = None,
        maximum_redirects: int = 5,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
    ) -> None:
        self.artifacts = artifacts
        self.enabled = enabled
        self.allowed_domains = tuple(value.lower().rstrip(".") for value in allowed_domains)
        self.allowed_ports = frozenset(allowed_ports)
        self.resolver = resolver or self._resolve
        self.transport = transport or self._transport
        self.maximum_redirects = maximum_redirects
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def fetch(
        self,
        request: DownloadRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> DownloadResult:
        started = time.monotonic()
        failure = self._policy_failure(request, decision)
        if failure is not None:
            return self._failed(request, started, failure)
        limits = decision.limits if isinstance(decision.limits, Mapping) else {}
        maximum = min(
            request.maximum_bytes,
            int(limits.get("max_download_bytes", request.maximum_bytes)),
        )
        current = request.url
        visited: set[str] = set()
        redirects: list[str] = []
        peers: list[str] = []
        try:
            for _hop in range(self.maximum_redirects + 1):
                if cancellation.cancelled():
                    return self._failed(
                        request,
                        started,
                        StableFailure(
                            code=StableFailureCode.CANCELLED,
                            message="download cancelled",
                        ),
                        status="cancelled",
                    )
                if time.monotonic() - started >= request.timeout_seconds:
                    raise TimeoutError("download total timeout exceeded")
                normalized, host, port = self._validate_url(current)
                if normalized in visited:
                    raise ValueError("redirect loop detected")
                visited.add(normalized)
                addresses = tuple(self.resolver(host, port))
                if not addresses:
                    raise ValueError("DNS returned no addresses")
                for address in addresses:
                    self._validate_address(address)
                peer = sorted(addresses)[0]
                remaining = request.timeout_seconds - (time.monotonic() - started)
                response = self.transport(
                    normalized,
                    peer,
                    min(self.connect_timeout, remaining),
                    min(self.read_timeout, remaining),
                )
                self._validate_address(response.peer_ip)
                if response.peer_ip != peer:
                    raise ValueError("transport peer differs from the validated pinned address")
                peers.append(peer)
                headers = {key.lower(): value for key, value in response.headers.items()}
                if response.status in {301, 302, 303, 307, 308}:
                    location = headers.get("location")
                    response.body.close()
                    if not location:
                        raise ValueError("redirect response is missing Location")
                    redirects.append(normalized)
                    current = urljoin(normalized, location)
                    continue
                if response.status < 200 or response.status >= 300:
                    response.body.close()
                    raise ValueError(f"download returned HTTP status {response.status}")
                content_length = headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        response.body.close()
                        raise ValueError("invalid Content-Length") from error
                    if declared_length < 0 or declared_length > maximum:
                        response.body.close()
                        raise ValueError("download exceeds byte limit")
                content = self._read_bounded(response.body, maximum, cancellation, started, request)
                actual = hashlib.sha256(content).hexdigest()
                if request.expected_sha256 is not None and actual != request.expected_sha256:
                    raise _IntegrityError("download checksum mismatch")
                media_type = headers.get(
                    "content-type", "application/octet-stream"
                ).split(";", 1)[0]
                if (
                    request.expected_media_type is not None
                    and media_type != request.expected_media_type
                ):
                    raise ValueError("download media type mismatch")
                artifact = self.artifacts.put(
                    io.BytesIO(content),
                    ArtifactPutRequest(
                        id=identifier("artifact-request"),
                        run_id=request.run_id,
                        created_at=now(),
                        media_type=media_type,
                        logical_kind=request.destination_kind,
                        producer_action_id=request.id,
                        source=freeze_json(
                            {
                                "initial_url": request.url,
                                "final_url": normalized,
                                "redirects": redirects,
                                "peers": peers,
                                "sha256": actual,
                                "policy_digest": decision.effective_policy_digest,
                            }
                        ),
                        redacted=bool(request.secret_bindings),
                    ),
                )
                return DownloadResult(
                    id=identifier("download"),
                    run_id=request.run_id,
                    created_at=now(),
                    request_digest=request.content_digest or "",
                    status="succeeded",
                    duration_seconds=time.monotonic() - started,
                    resource_usage=freeze_json(
                        {"bytes": len(content), "redirect_count": len(redirects)}
                    ),
                    artifact=artifact,
                    final_url=normalized,
                )
            raise ValueError("redirect limit exceeded")
        except _DownloadCancelled as error:
            return self._failed(
                request,
                started,
                StableFailure(code=StableFailureCode.CANCELLED, message=str(error)),
                status="cancelled",
            )
        except TimeoutError as error:
            code = StableFailureCode.TIMEOUT
            message = str(error)
        except _IntegrityError as error:
            code = StableFailureCode.INTEGRITY_FAILED
            message = str(error)
        except (ValueError, OSError) as error:
            code = StableFailureCode.NETWORK_BLOCKED
            message = str(error)
        return self._failed(request, started, StableFailure(code=code, message=message))

    def _validate_url(self, value: str) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ValueError("only absolute HTTPS URLs are permitted")
        if parsed.fragment:
            raise ValueError("URL fragments are forbidden")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL userinfo is forbidden")
        host = parsed.hostname.lower().rstrip(".")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("IP-literal URLs are forbidden")
        port = parsed.port or 443
        if port not in self.allowed_ports:
            raise ValueError("URL port is not allowlisted")
        allowed = any(
            host == rule
            or (rule.startswith(".") and host.endswith(rule) and host != rule[1:])
            for rule in self.allowed_domains
        )
        if not allowed:
            raise ValueError("URL domain is not allowlisted")
        netloc = host if port == 443 else f"{host}:{port}"
        return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, "")), host, port

    @staticmethod
    def _validate_address(value: str) -> None:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            raise ValueError("DNS address is not globally routable")

    @staticmethod
    def _resolve(host: str, port: int) -> Sequence[str]:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(sorted({str(item[4][0]) for item in answers}))

    @staticmethod
    def _transport(
        url: str, peer_ip: str, connect_timeout: float, read_timeout: float
    ) -> TransportResponse:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        raw = socket.create_connection((peer_ip, port), timeout=connect_timeout)
        context = ssl.create_default_context()
        tls = context.wrap_socket(raw, server_hostname=host)
        tls.settimeout(read_timeout)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
            "Connection: close\r\nAccept: */*\r\n\r\n"
        )
        tls.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(tls)
        response.begin()
        return TransportResponse(response.status, dict(response.getheaders()), response, peer_ip)

    @staticmethod
    def _read_bounded(
        body: BinaryIO,
        maximum: int,
        cancellation: Cancellation,
        started: float,
        request: DownloadRequest,
    ) -> bytes:
        output = bytearray()
        try:
            while True:
                if cancellation.cancelled():
                    raise _DownloadCancelled("download cancelled")
                if time.monotonic() - started >= request.timeout_seconds:
                    raise TimeoutError("download total timeout exceeded")
                chunk = body.read(min(64 * 1024, maximum - len(output) + 1))
                if not chunk:
                    return bytes(output)
                output.extend(chunk)
                if len(output) > maximum:
                    raise ValueError("download exceeds byte limit")
        finally:
            body.close()

    def _policy_failure(
        self, request: DownloadRequest, decision: PolicyDecision
    ) -> StableFailure | None:
        if not self.enabled or decision.request_digest != request.content_digest:
            return StableFailure(
                code=StableFailureCode.NETWORK_BLOCKED,
                message="download disabled or digest mismatch",
            )
        if request.secret_bindings:
            return StableFailure(
                code=StableFailureCode.NETWORK_BLOCKED,
                message="credential-bound downloads are unavailable on this restricted client",
            )
        if decision.outcome is DecisionOutcome.DENY:
            return StableFailure(
                code=StableFailureCode.POLICY_DENIED,
                message="download denied by policy",
            )
        if decision.outcome is DecisionOutcome.APPROVAL_REQUIRED:
            return StableFailure(
                code=StableFailureCode.APPROVAL_REQUIRED,
                message="download requires approval",
            )
        return None

    def _failed(
        self,
        request: DownloadRequest,
        started: float,
        failure: StableFailure,
        *,
        status: Literal["failed", "cancelled"] = "failed",
    ) -> DownloadResult:
        return DownloadResult(
            id=identifier("download"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status=status,
            failure=failure,
            duration_seconds=time.monotonic() - started,
        )
