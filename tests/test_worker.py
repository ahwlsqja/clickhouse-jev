"""Worker의 실행 경계와 스트림 출력 계약을 검증한다."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from clickhouse_jev import worker
from clickhouse_jev import __main__ as cli
from clickhouse_jev.protocol import ContractError
from clickhouse_jev.transport import Config


def config(**overrides):
    values = dict(endpoint='https://example.invalid/v1/systemone', model='request-model',
                  expected_model='response-model', batch_size=2, concurrency=3,
                  max_block_rows=10000, max_row_chars=10000, max_request_bytes=1000000,
                  deadline_seconds=2.0, state_format='string', allow_http=False)
    values.update(overrides)
    return Config(**values)


def payload(row, question='test', kind='noul', options=()):
    return dict(row_json=json.dumps(row, ensure_ascii=False), question=question,
                kind=kind, options_json=json.dumps(list(options), ensure_ascii=False))


def response(body):
    state = json.loads(body['state']) if isinstance(body['state'], str) else body['state']
    answers = {}
    for i, row in enumerate(state['rows']):
        kind = body['questions']['r%d' % i]['type']
        if kind == 'noul':
            answer = {'type': kind, 'noul': row['value']}
        elif kind == 'choice':
            answer = {'type': kind, 'choice': 'yes', 'confidence': 1.0,
                      'probabilities': {'yes': 1.0, 'no': 0.0}}
        else:
            answer = {'type': kind, 'score': 0.0, 'confidence': 1.0,
                      'legend': {'0': 'low', '1': 'high'}, 'probabilities': {'0': 1.0, '1': 0.0}}
        answers['r%d' % i] = answer
    return {'model': 'response-model', 'answers': answers}


class WorkerTests(unittest.TestCase):
    def test_module_cli_with_real_protocol_and_transport_over_loopback(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                encoded = json.dumps(response(body)).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            stream = '2\n' + ''.join(json.dumps(payload({'value': v})) + '\n' for v in (.1, .2))
            result = subprocess.run([sys.executable, '-m', 'clickhouse_jev',
                                     '--endpoint', 'http://127.0.0.1:%d/v1' % server.server_port,
                                     '--allow-http', '--model', 'request-model',
                                     '--expected-model', 'response-model'], input=stream,
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            answers = [json.loads(json.loads(line)['result']) for line in result.stdout.splitlines()]
            self.assertEqual([a['noul'] for a in answers], [.1, .2])
            self.assertEqual(result.stderr, '')
            self.assertIsInstance(requests[0]['state'], str)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_envelope_rejects_duplicate_keys_nonfinite_and_overflow(self):
        valid = json.dumps(payload({'value': .1}))
        cases = [valid[:-1] + ',"question":"duplicate-question"}',
                 valid[:-1] + ',"extra":NaN}', valid[:-1] + ',"extra":Infinity}',
                 valid[:-1] + ',"extra":1e999}']
        for line in cases:
            with self.subTest(line=line):
                calls, out = [], io.StringIO()
                with self.assertRaises(ContractError):
                    worker.serve(io.StringIO('1\n' + line + '\n'), out, config(),
                                 lambda body, timeout: calls.append(body) or response(body))
                self.assertEqual(calls, [])
                self.assertEqual(out.getvalue(), '')

    def test_cli_explicit_env_key_is_consumed_once_and_exclusive_with_fd(self):
        args = ['--endpoint', 'https://example.invalid/v1', '--model', 'request-model',
                '--expected-model', 'response-model', '--api-key-env', 'DUMMY_JEV_KEY']
        environment = {'DUMMY_JEV_KEY': 'dummy-key', 'UNRELATED': 'untouched'}
        keys = []
        with mock.patch.object(os, 'environ', environment), \
             mock.patch.object(cli, 'HttpTransport', lambda cfg, key: keys.append(key) or object()), \
             mock.patch.object(cli, 'serve'):
            self.assertEqual(cli.main(args), 0)
        self.assertEqual(keys, ['dummy-key'])
        self.assertEqual(environment, {'UNRELATED': 'untouched'})
        for environment, extra in (({}, []), ({'DUMMY_JEV_KEY': 'invalid\nkey'}, []),
                                   ({'DUMMY_JEV_KEY': 'dummy-key'}, ['--api-key-fd', '42'])):
            stderr = io.StringIO()
            with mock.patch.object(os, 'environ', environment), mock.patch('sys.stderr', stderr):
                self.assertNotEqual(cli.main(args + extra), 0)
            self.assertEqual(stderr.getvalue(), 'jev_worker_failed\n')

    def test_cli_connects_all_config_fields_and_fd_without_exposing_failures(self):
        args = ['--endpoint', 'http://127.0.0.1:1/v1', '--allow-http',
                '--model', 'request-model', '--expected-model', 'response-model',
                '--batch-size', '3', '--concurrency', '2', '--max-block-rows', '10',
                '--max-row-chars', '100', '--max-request-bytes', '900',
                '--deadline-seconds', '1.5', '--state-format', 'object', '--api-key-fd', '42']
        called = []
        with mock.patch.object(cli, 'serve', lambda inp, out, cfg, transport: called.append(cfg), create=True), \
             mock.patch.object(cli, 'HttpTransport', lambda cfg, key: ('transport', key), create=True), \
             mock.patch.object(cli, 'read_api_key', return_value='dummy-key', create=True) as reader:
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(len(called), 1)
            reader.assert_called_once_with(42)
        self.assertEqual((called[0].batch_size, called[0].concurrency, called[0].state_format), (3, 2, 'object'))
        self.assertEqual((called[0].max_block_rows, called[0].max_row_chars, called[0].max_request_bytes), (10, 100, 900))
        self.assertEqual(called[0].deadline_seconds, 1.5)
        stderr = io.StringIO()
        with mock.patch.object(cli, 'serve', side_effect=RuntimeError('private-remote-key'), create=True), \
             mock.patch.object(cli, 'read_api_key', return_value='dummy-key', create=True), \
             mock.patch('sys.stderr', stderr):
            self.assertNotEqual(cli.main(args), 0)
        self.assertEqual(stderr.getvalue(), 'jev_worker_failed\n')

    def test_malformed_header_truncated_block_blank_or_invalid_json_is_safe(self):
        item = json.dumps(payload({'value': .1})) + '\n'
        for stream in ('-1\n', 'private-header\n', '\n', '+1\n', '10001\n',
                       '2\n' + item, '1\n\n', '1\nprivate-json\n', '1\n[]\n'):
            with self.subTest(stream=stream):
                calls, out = [], io.StringIO()
                with self.assertRaises(ContractError) as error:
                    worker.serve(io.StringIO(stream), out, config(),
                                 lambda body, timeout: calls.append(body) or response(body))
                self.assertEqual(calls, [])
                self.assertEqual(out.getvalue(), '')
                self.assertNotIn('private', str(error.exception))

    def test_stream_readline_is_bounded_before_materializing_invalid_row(self):
        class GuardedInput(io.StringIO):
            def readline(self, size=-1):
                if size < 0:
                    raise AssertionError('unbounded readline')
                return super().readline(size)

        with self.assertRaises(ContractError):
            worker.serve(GuardedInput('1\n' + 'x' * 100000 + '\n'), io.StringIO(),
                         config(max_request_bytes=100, max_row_chars=10), response)

    def test_multiple_zero_duplicate_unicode_blocks_preserve_results(self):
        item = payload({'value': .1, 'blank': '', 'label': '한글 ☃'}, kind='choice', options=('예', '아니오'))
        stream = '0\n2\n' + json.dumps(item, ensure_ascii=False) + '\n' + json.dumps(item, ensure_ascii=False) + '\n0\n1\n' + json.dumps(item, ensure_ascii=False) + '\n'
        seen = []

        def transport(body, timeout):
            state = json.loads(body['state'])
            seen.extend(state['rows'])
            return {'model': 'response-model', 'answers': {
                'r%d' % i: {'type': 'choice', 'choice': '예', 'confidence': 1.0,
                           'probabilities': {'예': 1.0, '아니오': 0.0}}
                for i in range(len(state['rows']))}}

        out = io.StringIO()
        worker.serve(io.StringIO(stream), out, config(), transport)
        answers = [json.loads(json.loads(line)['result']) for line in out.getvalue().splitlines()]
        self.assertEqual([a['choice'] for a in answers], ['예', '예', '예'])
        self.assertEqual(seen, [{'value': .1, 'blank': '', 'label': '한글 ☃'}] * 3)
        self.assertIn('예', out.getvalue())

    def test_failed_answer_or_transport_has_no_partial_block_output(self):
        inputs = [payload({'value': .1}), payload({'value': .2})]
        encoded = '2\n' + ''.join(json.dumps(p) + '\n' for p in inputs)
        for failure in ('missing', 'exception'):
            with self.subTest(failure=failure):
                out = io.StringIO()

                def transport(body, timeout):
                    state = json.loads(body['state'])
                    if state['rows'][0]['value'] == .2:
                        if failure == 'exception':
                            raise RuntimeError('secret-remote-row')
                        return {'model': 'response-model', 'answers': {}}
                    return response(body)

                with self.assertRaises(ContractError) as error:
                    worker.serve(io.StringIO(encoded), out, config(batch_size=1), transport)
                self.assertEqual(out.getvalue(), '')
                self.assertNotIn('secret-remote-row', str(error.exception))

    def test_deadline_returns_without_waiting_for_uncooperative_transport(self):
        release = threading.Event()
        started = time.monotonic()
        try:
            with self.assertRaises(ContractError):
                worker.evaluate_block([payload({'value': .1})], config(deadline_seconds=.03),
                                      lambda body, timeout: release.wait(.5) or response(body))
            self.assertLess(time.monotonic() - started, .2)
        finally:
            release.set()

    def test_timeout_shrinks_as_the_same_block_advances(self):
        timeouts = []

        def transport(body, timeout):
            timeouts.append(timeout)
            time.sleep(.03)
            return response(body)

        worker.evaluate_block([payload({'value': .1}), payload({'value': .2})],
                              config(batch_size=1, concurrency=1, deadline_seconds=.5), transport)
        self.assertLess(timeouts[1], timeouts[0] - .02)

    def test_unfinished_submitted_futures_never_exceed_concurrency(self):
        release = threading.Event()
        peak = []
        original_pool = worker.ThreadPoolExecutor

        class TrackingPool(original_pool):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.submitted = []

            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                self.submitted.append(future)
                peak.append(sum(not f.done() for f in self.submitted))
                return future

        def transport(body, timeout):
            release.wait(.5)
            return response(body)

        timer = threading.Timer(.05, release.set)
        timer.start()
        try:
            with mock.patch.object(worker, 'ThreadPoolExecutor', TrackingPool):
                answers = worker.evaluate_block([payload({'value': .1}) for _ in range(12)],
                                                config(batch_size=1, concurrency=2), transport)
            self.assertEqual(len(answers), 12)
            self.assertLessEqual(max(peak), 2)
        finally:
            release.set()
            timer.cancel()

    def test_every_request_byte_limit_checked_before_any_transport(self):
        calls = []
        inputs = [payload({'value': .1}), payload({'value': .2}, question='한' * 1000)]
        with self.assertRaises(ContractError):
            worker.evaluate_block(inputs, config(max_request_bytes=500),
                                  lambda body, timeout: calls.append(body) or response(body))
        self.assertEqual(calls, [])

    def test_all_inputs_and_block_limit_checked_before_transport(self):
        calls = []
        transport = lambda body, timeout: calls.append(body) or response(body)
        with self.assertRaises(ContractError):
            worker.evaluate_block([payload({'value': .1}), payload({'value': .2})],
                                  config(max_block_rows=1), transport)
        self.assertEqual(calls, [])
        invalid = payload({'value': .1})
        invalid['row_json'] = 'private-invalid-row'
        with self.assertRaises(ContractError) as error:
            worker.evaluate_block([payload({'value': .1}), invalid], config(), transport)
        self.assertNotIn('private-invalid-row', str(error.exception))
        self.assertEqual(calls, [])
        with self.assertRaises(ContractError):
            worker.evaluate_block([payload({'value': .1})], config(max_row_chars=1), transport)
        self.assertEqual(calls, [])

    def test_mixed_groups_restore_positions_after_reverse_completion(self):
        inputs = [payload({'value': .1}), payload({'value': .2}, 'other'),
                  payload({'value': .3}, kind='choice', options=('yes', 'no')),
                  payload({'value': .4}), payload({'value': .5}, kind='score', options=('low', 'high')),
                  payload({'value': .1})]
        calls = []
        completed = []
        other_completed = threading.Event()

        def transport(body, timeout):
            calls.append(body)
            state = json.loads(body['state'])
            if state['rows'][0]['value'] == .1:
                other_completed.wait(1)
            completed.append(state['rows'][0]['value'])
            if state['rows'][0]['value'] == .2:
                other_completed.set()
            return response(body)

        answers = worker.evaluate_block(inputs, config(), transport)
        self.assertEqual([a['type'] for a in answers], ['noul', 'noul', 'choice', 'noul', 'score', 'noul'])
        self.assertEqual([answers[i]['noul'] for i in (0, 1, 3, 5)], [.1, .2, .4, .1])
        self.assertEqual(len(calls), 5)
        self.assertEqual(completed[0], .2)
        self.assertTrue(all(len(json.loads(b['state'])['rows']) <= 2 for b in calls))


if __name__ == '__main__':
    unittest.main()
