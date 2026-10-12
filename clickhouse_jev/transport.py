"""Jev HTTP 전송의 설정과 안전한 자격증명 입력."""

from dataclasses import dataclass
import http.client
import json
import math
import os
import socket
import threading
import time
from urllib.parse import urlsplit

MAX_API_KEY_BYTES = 4096
MAX_RESPONSE_BYTES = 1000000


class TransportError(ValueError):
    """원격 응답이나 credential을 포함하지 않는 전송 오류."""


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_number")
    return number


def _invalid_constant(value):
    raise ValueError("nonfinite_number")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_response_key")
        result[name] = value
    return result


@dataclass(frozen=True)
class Config:
    """전송 설정의 공개 인터페이스."""

    endpoint: str
    model: str
    expected_model: str
    batch_size: int = 20
    concurrency: int = 4
    max_block_rows: int = 10000
    max_row_chars: int = 10000
    max_request_bytes: int = 1000000
    deadline_seconds: float = 30.0
    state_format: str = "string"
    allow_http: bool = False

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip()
               for v in (self.model, self.expected_model)):
            raise TransportError("invalid_config_model")
        if self.state_format not in ("string", "object") or type(self.allow_http) is not bool:
            raise TransportError("invalid_config_format")
        for name in ("batch_size", "concurrency", "max_block_rows",
                     "max_row_chars", "max_request_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise TransportError("invalid_config_limit")
        if type(self.deadline_seconds) not in (int, float) or self.deadline_seconds <= 0:
            raise TransportError("invalid_config_deadline")
        try:
            finite_deadline = math.isfinite(self.deadline_seconds)
        except OverflowError:
            raise TransportError("invalid_config_deadline") from None
        if not finite_deadline:
            raise TransportError("invalid_config_deadline")
        if (not isinstance(self.endpoint, str) or not self.endpoint
                or any(ord(c) <= 32 or ord(c) == 127 for c in self.endpoint)
                or "#" in self.endpoint):
            raise TransportError("invalid_endpoint")
        try:
            url = urlsplit(self.endpoint)
            valid_port = url.port is None or 1 <= url.port <= 65535
        except ValueError:
            raise TransportError("invalid_endpoint") from None
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None or not valid_port
                or (url.scheme == "http" and self.allow_http is not True)):
            raise TransportError("invalid_endpoint")


def validate_api_key(value: str) -> str:
    """공개 credential 입력 경로가 공유하는 정규화 계약."""
    if not isinstance(value, str):
        raise TransportError("invalid_credential")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise TransportError("invalid_credential") from None
    if size > MAX_API_KEY_BYTES:
        raise TransportError("credential_too_large")
    key = value.removesuffix("\n").removesuffix("\r")
    if not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise TransportError("invalid_credential")
    return key


def read_api_key(fd: int) -> str:
    """호출자가 제공한 전용 FD를 한 번 소비한다."""
    if type(fd) is not int or fd < 0:
        raise TransportError("invalid_credential_fd")
    try:
        chunks = bytearray()
        while True:
            chunk = os.read(fd, MAX_API_KEY_BYTES + 1 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_API_KEY_BYTES:
                raise TransportError("credential_too_large")
        return validate_api_key(chunks.decode("utf-8"))
    except (OSError, UnicodeError):
        raise TransportError("credential_read_failed") from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class HttpTransport:
    """한 번의 Jev HTTP 요청을 처리한다."""

    def __init__(self, config: Config, api_key=None):
        self.config = config
        self.api_key = None if api_key is None else validate_api_key(api_key)

    def __call__(self, body: dict, timeout: float) -> dict:
        if type(timeout) not in (int, float):
            raise TransportError("invalid_timeout")
        try:
            budget = min(float(timeout), self.config.deadline_seconds)
        except OverflowError:
            raise TransportError("invalid_timeout") from None
        if not math.isfinite(float(timeout)) or budget <= 0 or budget > threading.TIMEOUT_MAX:
            raise TransportError("invalid_timeout")
        deadline = time.monotonic() + budget

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TransportError("request_deadline_exceeded")
            return seconds

        if not isinstance(body, dict):
            raise TransportError("invalid_request")
        try:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise TransportError("invalid_request") from None
        if len(payload) > self.config.max_request_bytes:
            raise TransportError("request_too_large")
        url = urlsplit(self.config.endpoint)
        connection_type = (http.client.HTTPSConnection if url.scheme == "https"
                           else http.client.HTTPConnection)
        connection = connection_type(url.hostname, url.port, timeout=remaining())
        headers = {"Content-Type": "application/json"}
        if self.api_key is not None:
            headers["Authorization"] = "Bearer " + self.api_key
        path = (url.path or "/") + ("?" + url.query if url.query else "")
        active_socket = [None]
        response = None

        def stop_socket():
            sock = active_socket[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(remaining(), stop_socket)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            active_socket[0] = connection.sock
            active_socket[0].settimeout(remaining())
            connection.request("POST", path, body=payload, headers=headers)
            active_socket[0].settimeout(remaining())
            response = connection.getresponse()
            if response.status != 200:
                raise TransportError("http_status_%d" % response.status)
            content_length = response.getheader("Content-Length")
            if content_length is not None and int(content_length) > MAX_RESPONSE_BYTES:
                raise TransportError("response_too_large")
            raw = bytearray()
            while True:
                active_socket[0].settimeout(remaining())
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    if response.length not in (None, 0):
                        raise TransportError("incomplete_response")
                    break
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TransportError("response_too_large")
                if response.length == 0 or response.isclosed():
                    break
            remaining()
            data = json.loads(raw.decode("utf-8"), parse_float=_finite_float,
                              parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
            if not isinstance(data, dict):
                raise TransportError("invalid_response")
            remaining()
            return data
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, TransportError):
                raise
            raise TransportError("request_failed") from None
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()
