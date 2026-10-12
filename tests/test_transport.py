"""실제 credential이나 외부 네트워크 없이 전송 경계를 검증한다."""
import dataclasses
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import os
import socket
import threading
import time
import unittest

from clickhouse_jev.transport import (Config, HttpTransport, TransportError,
                                    MAX_RESPONSE_BYTES, read_api_key, validate_api_key)


@contextmanager
def mock_server(response=b'{"ok":true}', status=200, headers=None, responder=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = self.rfile.read(int(self.headers['Content-Length']))
            received.append({'body': payload, 'path': self.path,
                             'headers': dict(self.headers), 'method': self.command})
            try:
                if responder is not None:
                    responder(self)
                    return
                self.send_response(status)
                self.send_header('Content-Length', str(len(response)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d/api?version=1' % server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class ConfigTests(unittest.TestCase):
    def test_unrepresentable_integer_deadline_is_a_safe_config_error(self):
        with self.assertRaises(TransportError):
            Config('https://example.invalid/api', 'jev', 'jev', deadline_seconds=10**1000)

    def test_model_and_format_configuration_are_validated(self):
        base = Config('https://example.invalid/api', 'jev', 'jev')
        for field in ('model', 'expected_model'):
            for value in ('', '   ', None, True):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TransportError):
                        dataclasses.replace(base, **{field: value})
        for value in ('JSON', '', None, True):
            with self.subTest(state_format=value):
                with self.assertRaises(TransportError):
                    dataclasses.replace(base, state_format=value)
        for value in (1, 'true', None):
            with self.subTest(allow_http=value):
                with self.assertRaises(TransportError):
                    dataclasses.replace(base, allow_http=value)
        self.assertEqual(dataclasses.replace(base, state_format='object').state_format, 'object')

    def test_endpoint_requires_explicit_http_and_rejects_unsafe_url_parts(self):
        for endpoint in ('http://127.0.0.1/api', 'ftp://example.invalid/api',
                         'https:///api', 'https://u:p@example.invalid/api',
                         'https://@example.invalid/api', 'https://example.invalid/api#fragment',
                         'https://example.invalid/api#', 'https://example.invalid:99999/api',
                         'https://[broken/api', ' https://example.invalid/api',
                         'https://example.invalid/\nsecret', None):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(TransportError):
                    Config(endpoint, 'jev', 'jev')
        config = Config('http://127.0.0.1:8765/api?version=1', 'jev', 'jev', allow_http=True)
        self.assertTrue(config.allow_http)

    def test_numeric_limits_reject_invalid_bounds(self):
        base = Config('https://example.invalid/api', 'jev', 'jev')
        for field in ('batch_size', 'concurrency', 'max_block_rows',
                      'max_row_chars', 'max_request_bytes'):
            for value in (0, -1, True, 1.5, '20', None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TransportError):
                        dataclasses.replace(base, **{field: value})
        for value in (0, -1, True, float('nan'), float('inf'), '30', None):
            with self.subTest(deadline=value):
                with self.assertRaises(TransportError):
                    dataclasses.replace(base, deadline_seconds=value)
        self.assertEqual(dataclasses.replace(base, deadline_seconds=.001).deadline_seconds, .001)

    def test_config_signature_defaults_and_frozen_contract(self):
        names = ['endpoint', 'model', 'expected_model', 'batch_size', 'concurrency',
                 'max_block_rows', 'max_row_chars', 'max_request_bytes',
                 'deadline_seconds', 'state_format', 'allow_http']
        self.assertEqual(list(inspect.signature(Config).parameters), names)
        config = Config('https://example.invalid/v1/systemone', 'jev', 'jev')
        self.assertEqual((config.batch_size, config.concurrency, config.max_block_rows,
                          config.max_row_chars, config.max_request_bytes,
                          config.deadline_seconds, config.state_format, config.allow_http),
                         (20, 4, 10000, 10000, 1000000, 30.0, 'string', False))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.model = 'changed'


class CredentialTests(unittest.TestCase):
    def test_shared_key_validation_normalizes_once_and_guards_direct_input(self):
        self.assertEqual(validate_api_key('test-key\r\n'), 'test-key')
        self.assertEqual(validate_api_key('x'*4096), 'x'*4096)
        for value in ('', ' ', 'CREDENTIAL_SECRET\nsecond', 'CREDENTIAL_SECRET\n\n',
                      'x'*4097, '\ud800', '한글', None, True):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(TransportError) as caught:
                    validate_api_key(value)
                self.assertNotIn('CREDENTIAL_SECRET', str(caught.exception))
                if value is not None:
                    with self.assertRaises(TransportError):
                        HttpTransport(Config('https://example.invalid/api', 'jev', 'jev'), value)
        with mock_server() as (endpoint, received):
            config = Config(endpoint, 'jev', 'jev', allow_http=True)
            HttpTransport(config, 'test-key\r\n')({}, 1)
            self.assertEqual(received[0]['headers']['Authorization'], 'Bearer test-key')

    def test_invalid_or_oversized_credential_is_redacted_and_fd_is_closed(self):
        for payload in (b'', b'   ', b'\xff', b'DO_NOT_PRINT\nsecond',
                        b'DO_NOT_PRINT\x00', b'DO_NOT_PRINT\n\n', b'x'*4097):
            with self.subTest(size=len(payload)):
                fd, writer = os.pipe()
                os.write(writer, payload)
                os.close(writer)
                try:
                    with self.assertRaises(TransportError) as caught:
                        read_api_key(fd)
                    self.assertNotIn('DO_NOT_PRINT', str(caught.exception))
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        fd, writer = os.pipe()
        os.write(writer, b'x'*4096)
        os.close(writer)
        self.assertEqual(len(read_api_key(fd)), 4096)

    def test_fd_is_consumed_once_and_closed_after_success(self):
        fd, writer = os.pipe()
        os.write(writer, b'test-key\r\n')
        os.close(writer)
        try:
            self.assertEqual(read_api_key(fd), 'test-key')
            with self.assertRaises(OSError):
                os.fstat(fd)
            with self.assertRaises(TransportError):
                read_api_key(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


class HttpTests(unittest.TestCase):
    def test_duplicate_response_object_keys_are_rejected_before_validation(self):
        payloads = (
            b'{"model":"wrong","model":"jev"}',
            b'{"answers":{"r0":{},"r0":{"type":"noul","noul":0.9}}}',
            b'{"answers":{"r0":{"type":"noul","noul":-1,"noul":0.9}}}',
            b'{"nested":[{"REMOTE_BODY_SECRET":1,"REMOTE_BODY_SECRET":2}]}',
        )
        for payload in payloads:
            with self.subTest(payload_size=len(payload)):
                with mock_server(payload) as (endpoint, received):
                    transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
                    with self.assertRaises(TransportError) as caught:
                        transport({}, 1)
                    self.assertNotIn('REMOTE_BODY_SECRET', str(caught.exception))
                    self.assertEqual(len(received), 1)

    def test_total_deadline_does_not_extend_with_continuous_response_bytes(self):
        payload = b'{"padding":"' + b'x'*80 + b'"}'
        def drip(handler):
            handler.send_response(200)
            handler.send_header('Content-Length', str(len(payload)))
            handler.end_headers()
            for byte in payload:
                handler.wfile.write(bytes([byte]))
                time.sleep(.01)
        for configured, remaining in ((.06, 1), (1, .06)):
            with self.subTest(configured=configured, remaining=remaining):
                with mock_server(responder=drip) as (endpoint, received):
                    config = Config(endpoint, 'jev', 'jev', allow_http=True,
                                    deadline_seconds=configured)
                    started = time.monotonic()
                    with self.assertRaises(TransportError):
                        HttpTransport(config)({}, remaining)
                    self.assertLess(time.monotonic()-started, .35)
                    self.assertEqual(len(received), 1)
        def delayed_headers(handler):
            time.sleep(.2)
            handler.send_response(200)
            handler.send_header('Content-Length', '2')
            handler.end_headers()
            handler.wfile.write(b'{}')
        with mock_server(responder=delayed_headers) as (endpoint, received):
            config = Config(endpoint, 'jev', 'jev', allow_http=True, deadline_seconds=.04)
            started = time.monotonic()
            with self.assertRaises(TransportError):
                HttpTransport(config)({}, 1)
            self.assertLess(time.monotonic()-started, .15)
            self.assertEqual(len(received), 1)
        with mock_server() as (endpoint, received):
            transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
            for timeout in (0, -1, True, float('nan'), float('inf'), '1', None):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(TransportError):
                        transport({}, timeout)
            self.assertEqual(received, [])

    def test_malformed_nonfinite_or_nonobject_json_response_is_redacted(self):
        for payload in (b'{"bad":"REMOTE_BODY_SECRET"', b'\xff', b'',
                        b'{"n":NaN}', b'{"n":Infinity}', b'{"n":-Infinity}',
                        b'{"nested":{"n":1e999}}', b'[]', b'"REMOTE_BODY_SECRET"',
                        b'null', b'1', b'true'):
            with self.subTest(payload_size=len(payload)):
                with mock_server(payload) as (endpoint, received):
                    transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
                    with self.assertRaises(TransportError) as caught:
                        transport({}, 1)
                    self.assertNotIn('REMOTE_BODY_SECRET', str(caught.exception))
                    self.assertEqual(len(received), 1)

    def test_response_bytes_are_capped_with_and_without_content_length(self):
        prefix, suffix = b'{"padding":"', b'"}'
        valid = prefix + b'x'*(MAX_RESPONSE_BYTES-len(prefix)-len(suffix)) + suffix
        with mock_server(valid) as (endpoint, received):
            transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
            self.assertEqual(len(transport({}, 1)['padding']),
                             MAX_RESPONSE_BYTES-len(prefix)-len(suffix))
            self.assertEqual(len(received), 1)
        oversized = prefix + b'x'*MAX_RESPONSE_BYTES + suffix
        with mock_server(oversized) as (endpoint, received):
            transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
            with self.assertRaises(TransportError):
                transport({}, 1)
            self.assertEqual(len(received), 1)
        def no_length(handler):
            handler.send_response(200)
            handler.end_headers()
            handler.wfile.write(oversized)
        with mock_server(responder=no_length) as (endpoint, received):
            transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
            with self.assertRaises(TransportError):
                transport({}, 1)
            self.assertEqual(len(received), 1)

    def test_oversized_or_nonfinite_request_never_reaches_http(self):
        body = {'row': '한글'}
        payload = json.dumps(body, ensure_ascii=False, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')
        with mock_server() as (endpoint, received):
            config = Config(endpoint, 'jev', 'jev', max_request_bytes=len(payload), allow_http=True)
            self.assertEqual(HttpTransport(config)(body, 1), {'ok': True})
            self.assertEqual(len(received), 1)
            cyclic = []
            cyclic.append(cyclic)
            for invalid in ({'row': '한글추가'}, {'n': float('nan')}, {'n': float('inf')},
                            {'n': object()}, {'n': cyclic}, {'n': '\ud800'}, ['ROW_SECRET']):
                with self.subTest(kind=type(invalid).__name__):
                    with self.assertRaises(TransportError) as caught:
                        HttpTransport(config)(invalid, 1)
                    self.assertNotIn('ROW_SECRET', str(caught.exception))
                    self.assertEqual(len(received), 1)

    def test_failures_and_redirects_never_retry_or_expose_sensitive_text(self):
        for status in (301, 302, 307, 308, 401, 422, 429, 500, 503):
            with self.subTest(status=status):
                with mock_server(b'{"error":"REMOTE_BODY_SECRET"}', status=status,
                                 headers={'Location': '/redirect-target'}) as (endpoint, received):
                    transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True),
                                              api_key='CREDENTIAL_SECRET')
                    with self.assertRaises(TransportError) as caught:
                        transport({'row': 'ROW_SECRET'}, 1)
                    self.assertEqual(len(received), 1)
                    for secret in ('REMOTE_BODY_SECRET', 'CREDENTIAL_SECRET', 'ROW_SECRET'):
                        self.assertNotIn(secret, str(caught.exception))
        def disconnect(handler):
            handler.connection.shutdown(socket.SHUT_RDWR)
        with mock_server(responder=disconnect) as (endpoint, received):
            transport = HttpTransport(Config(endpoint, 'jev', 'jev', allow_http=True))
            with self.assertRaises(TransportError):
                transport({'row': 'ROW_SECRET'}, 1)
            self.assertEqual(len(received), 1)

    def test_success_posts_compact_utf8_json_and_optional_bearer(self):
        body = {'row': {'이름': '한글'}, 'model': 'jev'}
        with mock_server() as (endpoint, received):
            config = Config(endpoint, 'jev', 'jev', allow_http=True)
            transport = HttpTransport(config, api_key='test-key')
            self.assertEqual(transport(body, 1), {'ok': True})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]['body'],
                             json.dumps(body, ensure_ascii=False, separators=(',', ':'),
                                        allow_nan=False).encode('utf-8'))
            self.assertEqual(received[0]['method'], 'POST')
            self.assertEqual(received[0]['path'], '/api?version=1')
            self.assertEqual(received[0]['headers']['Authorization'], 'Bearer test-key')
            self.assertEqual(received[0]['headers']['Content-Type'], 'application/json')
            self.assertEqual(HttpTransport(config)({}, 1), {'ok': True})
            self.assertNotIn('Authorization', received[1]['headers'])


if __name__ == '__main__':
    unittest.main()
