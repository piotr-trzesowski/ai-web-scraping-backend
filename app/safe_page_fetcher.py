"""Synchronous, robots-aware fetching of small public web pages."""

import fnmatch
import ipaddress
import math
import re
import socket
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

USER_AGENT = "EventBackend/0.1"
MAX_WAIT = 60.0
MAX_REDIRECTS = 5
ROBOTS_TTL = 3600.0
_REDIRECTS = {301, 302, 303, 307, 308}
_INTERNAL_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".lan", ".home",
    ".onion", ".invalid", ".test",
)


class FetchError(RuntimeError):
    """The page could not be fetched safely or politely."""


@dataclass
class _FetchContext:
    checking: set[str] = field(default_factory=set)
    redirects_left: int = MAX_REDIRECTS


def _robot_path(value: str) -> str:
    value = quote(value, safe="/%?=&:+,;@!$'()*-._~")
    # Decode only unreserved characters: an escaped slash is not a path slash.
    def normalize(match: re.Match) -> str:
        character = chr(int(match[0][1:], 16))
        return character if character.isascii() and (
            character.isalnum() or character in "-._~"
        ) else match[0].upper()
    return re.sub(r"%[0-9a-fA-F]{2}", normalize, value)


class _RobotsPolicy:
    def __init__(self, text: str = "", *, denied: bool = False) -> None:
        self.denied = denied
        self.interval = 0.0
        self.rules: list[tuple[re.Pattern, int, bool]] = []
        groups: list[tuple[list[str], list[tuple[str, str]]]] = []
        agents: list[str] = []
        records: list[tuple[str, str]] = []
        for line in text.lstrip("\ufeff").splitlines():
            key, separator, value = line.split("#", 1)[0].partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if records:
                    groups.append((agents, records))
                    agents, records = [], []
                agents.append(value.lower())
            elif agents and key in {"allow", "disallow", "crawl-delay", "request-rate"}:
                records.append((key, value))
        groups.append((agents, records))
        product = USER_AGENT.split("/")[0].lower()
        matches = [
            (max((len(agent) if agent == product else 0 for agent in names
                  if agent in {"*", product}), default=-1), directives)
            for names, directives in groups
        ]
        specificity = max((score for score, _ in matches), default=-1)
        for score, directives in matches:
            if score < 0 or score != specificity:
                continue
            for key, value in directives:
                if key in {"allow", "disallow"}:
                    if not value:
                        continue
                    if not value.startswith("/"):
                        raise FetchError("Invalid robots.txt path rule")
                    path = _robot_path(value)
                    anchored = path.endswith("$")
                    if anchored:
                        path = path[:-1]
                    # fnmatch's translated '*' uses atomic groups, avoiding the
                    # exponential backtracking of a naive wildcard regex.
                    pattern = "".join("[[]" if c == "[" else "[?]" if c == "?" else c for c in path)
                    if not anchored:
                        pattern += "*"
                    self.rules.append((
                        re.compile(fnmatch.translate(pattern)),
                        len(path) + int(anchored), key == "allow",
                    ))
                else:
                    try:
                        if key == "crawl-delay":
                            delay = float(value)
                        else:
                            count, period = value.split("/")
                            if int(count) <= 0 or float(period) <= 0:
                                raise ValueError("Nonpositive request rate")
                            delay = float(period) / int(count)
                    except (ValueError, OverflowError) as error:
                        raise FetchError(f"Invalid robots.txt {key}") from error
                    if not math.isfinite(delay) or delay < 0:
                        raise FetchError(f"Invalid robots.txt {key}")
                    self.interval = max(self.interval, delay)
        if self.interval > MAX_WAIT:
            raise FetchError("robots.txt requires a delay exceeding the 60-second wait limit")

    def can_fetch(self, url: httpx.URL) -> bool:
        if self.denied:
            return False
        path = _robot_path(url.raw_path.decode("ascii"))
        matches = [(size, allow) for pattern, size, allow in self.rules if pattern.match(path)]
        return max(matches, default=(0, True))[1]


def _validated_url(value: str | httpx.URL) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError) as error:
        raise FetchError("Invalid page URL") from error
    if url.scheme not in {"http", "https"} or not url.host:
        raise FetchError("Only absolute HTTP and HTTPS URLs are supported")
    if url.userinfo:
        raise FetchError("Credentials in URLs are not permitted")
    if url.port not in (None, 80 if url.scheme == "http" else 443):
        raise FetchError("Only the standard port for the URL scheme is permitted")
    host = url.host.lower().rstrip(".")
    if "%" in host:
        raise FetchError("Scoped or percent-escaped IP hosts are not permitted")
    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise FetchError("Local and internal hosts are not permitted")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise FetchError("Single-label hosts are not permitted") from None
    else:
        _require_public(address)
    return url.copy_with(host=host, fragment=None)


def _require_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ):
        raise FetchError("The host resolves to a non-public IP address")
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
        or address in ipaddress.ip_network("64:ff9b::/96")
        or address in ipaddress.ip_network("64:ff9b:1::/48")
    ):
        raise FetchError("IPv6 address translation and tunnelling are not supported")


def _public_ip(url: httpx.URL) -> str:
    try:
        address = ipaddress.ip_address(url.host)
    except ValueError:
        try:
            answers = socket.getaddrinfo(
                url.host, url.port or (443 if url.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as error:
            raise FetchError(f"DNS resolution failed for {url.host}") from error
        if not answers:
            raise FetchError("DNS returned no addresses")
        addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
    else:
        addresses = [address]
    # Reject mixed public/private answers rather than selecting a convenient one.
    for address in addresses:
        _require_public(address)
    return str(addresses[0])


class _PinnedTransport(httpx.BaseTransport):
    """Resolve once per request; connect to the literal IP, with original TLS SNI."""

    def __init__(self) -> None:
        # No pooled connections: two virtual hosts on one IP must never share TLS.
        self._transport = httpx.HTTPTransport(
            trust_env=False, retries=0,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = _validated_url(request.url)
        ip = _public_ip(url)
        host_header = f"[{url.host}]" if ":" in url.host else url.host
        headers = request.headers.copy()
        headers["Host"] = host_header
        # Never retain or replay session cookies, including across redirects.
        headers.pop("Cookie", None)
        pinned = httpx.Request(
            request.method, url.copy_with(host=ip), headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return self._transport.handle_request(pinned)

    def close(self) -> None:
        self._transport.close()


class PageFetcher:
    """Fetch public HTML/plain text without login, rendering, or paywall bypass.

    Instances are synchronous and not thread-safe. Robots policies are cached for
    one hour. Waits exceeding 60 seconds fail explicitly, rather than ignoring
    server policy. Unsupported content encodings are rejected.
    """

    def __init__(
        self, min_interval: float = 1.0, timeout: float = 10.0,
        max_bytes: int = 2_000_000, retries: int = 2,
    ) -> None:
        if not math.isfinite(min_interval) or not 0 <= min_interval <= MAX_WAIT:
            raise ValueError("min_interval must be between 0 and 60 seconds")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
            raise ValueError("retries must be an integer between 0 and 5")
        self.min_interval = min_interval
        self.max_bytes = max_bytes
        self.retries = retries
        self._last_request: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        self._not_before: dict[str, float] = {}
        self._robots: dict[str, tuple[float, _RobotsPolicy]] = {}
        self._client = httpx.Client(
            transport=_PinnedTransport(), trust_env=False,
            follow_redirects=False, timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        )

    def __enter__(self) -> "PageFetcher":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _origin(url: httpx.URL) -> str:
        return str(url.copy_with(path="/", query=None, fragment=None))

    def _wait(self, origin: str) -> None:
        now = time.monotonic()
        interval = max(self.min_interval, self._intervals.get(origin, 0))
        next_request = max(
            self._last_request.get(origin, now - interval) + interval,
            self._not_before.get(origin, now),
        )
        delay = next_request - now
        if delay > MAX_WAIT:
            raise FetchError("Required request delay exceeds the 60-second wait limit")
        if delay > 0:
            time.sleep(delay)
        self._last_request[origin] = time.monotonic()

    @staticmethod
    def _retry_delay(value: str | None, attempt: int) -> float:
        delay = float(2 ** attempt)
        if value is not None:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    seconds = (date - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError) as error:
                    raise FetchError("Invalid Retry-After header") from error
            if not math.isfinite(seconds):
                raise FetchError("Invalid Retry-After header")
            delay = max(delay, seconds)
        if delay > MAX_WAIT:
            raise FetchError("Retry-After exceeds the 60-second wait limit")
        return delay

    def _read_body(self, response: httpx.Response, robots: bool) -> bytes:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        allowed = {"text/plain"} if robots else {"text/html", "application/xhtml+xml", "text/plain"}
        if content_type not in allowed:
            raise FetchError(f"Unsupported content type: {content_type or '(missing)'}")
        length = response.headers.get("content-length")
        if length is not None:
            try:
                size = int(length)
            except ValueError as error:
                raise FetchError("Invalid Content-Length header") from error
            if size < 0 or size > self.max_bytes:
                raise FetchError("Response exceeds the configured size limit")
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "gzip", "deflate"}:
            raise FetchError(f"Unsupported content encoding: {encoding}")
        decoder = (
            zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS)
            if encoding != "identity" else None
        )
        result = bytearray()
        raw_size = 0
        try:
            for chunk in response.iter_raw(chunk_size=16_384):
                raw_size += len(chunk)
                if raw_size > self.max_bytes:
                    raise FetchError("Response exceeds the configured size limit")
                if decoder is not None:
                    chunk = decoder.decompress(chunk, self.max_bytes - len(result) + 1)
                result.extend(chunk)
                if len(result) > self.max_bytes:
                    raise FetchError("Decoded response exceeds the configured size limit")
                if decoder is not None and decoder.unused_data:
                    raise FetchError("Trailing or concatenated compressed streams are unsupported")
        except zlib.error as error:
            raise FetchError("Invalid compressed response") from error
        if decoder is not None and not decoder.eof:
            raise FetchError("Truncated compressed response")
        return bytes(result)

    def _request(self, url: httpx.URL, *, robots: bool) -> tuple[int, httpx.Headers, bytes]:
        origin = self._origin(url)
        for attempt in range(self.retries + 1):
            self._wait(origin)
            try:
                with self._client.stream("GET", url) as response:
                    status = response.status_code
                    if status == 429 or 500 <= status <= 599:
                        if attempt == self.retries:
                            raise FetchError(f"HTTP {status}: retry limit reached")
                        delay = self._retry_delay(response.headers.get("retry-after"), attempt)
                    else:
                        body = self._read_body(response, robots) if status == 200 else b""
                        return status, response.headers, body
            except httpx.TransportError as error:
                if attempt == self.retries:
                    raise FetchError("Network request failed after bounded retries") from error
                delay = self._retry_delay(None, attempt)
            self._not_before[origin] = time.monotonic() + delay
        raise AssertionError("Unreachable retry state")

    def _follow(
        self, url: httpx.URL, *, robots: bool, context: _FetchContext,
    ) -> tuple[int, httpx.Headers, bytes]:
        initial_origin = self._origin(url)
        while True:
            origin = self._origin(url)
            # robots.txt itself is the sole bootstrap exception for its origin.
            if not robots or origin != initial_origin:
                policy = self._robots_for(url, context)
                if not policy.can_fetch(url):
                    raise FetchError(f"robots.txt disallows {url}")
            status, headers, body = self._request(url, robots=robots)
            if status not in _REDIRECTS:
                return status, headers, body
            if context.redirects_left == 0:
                raise FetchError("Too many redirects")
            context.redirects_left -= 1
            location = headers.get("location")
            if not location:
                raise FetchError("Redirect is missing Location")
            try:
                url = _validated_url(url.join(location))
            except (httpx.InvalidURL, ValueError) as error:
                raise FetchError("Invalid redirect URL") from error
    def _robots_for(self, url: httpx.URL, context: _FetchContext) -> _RobotsPolicy:
        origin = self._origin(url)
        cached = self._robots.get(origin)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        if origin in context.checking:
            raise FetchError("Circular cross-origin robots.txt redirects")
        context.checking.add(origin)
        try:
            robots_url = httpx.URL(origin).join("robots.txt")
            status, _, body = self._follow(robots_url, robots=True, context=context)
            if status == 404:
                policy = _RobotsPolicy()
            elif status in {401, 403}:
                policy = _RobotsPolicy(denied=True)
            elif status == 200:
                policy = _RobotsPolicy(body.decode("utf-8", errors="replace"))
            else:
                raise FetchError(f"Unable to establish robots.txt policy: HTTP {status}")
            self._intervals[origin] = policy.interval
            self._robots[origin] = (time.monotonic() + ROBOTS_TTL, policy)
            return policy
        finally:
            context.checking.remove(origin)

    def fetch(self, url: str) -> str:
        if self._client.is_closed:
            raise FetchError("PageFetcher is closed")
        validated = _validated_url(url)
        status, headers, body = self._follow(validated, robots=False, context=_FetchContext())
        if status != 200:
            raise FetchError(f"Unable to fetch page: HTTP {status}")
        # Reconstruct only after enforcing both compressed and decoded size caps.
        headers = httpx.Headers(headers)
        headers.pop("content-encoding", None)
        return httpx.Response(200, headers=headers, content=body).text
