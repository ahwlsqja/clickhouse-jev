"""A deterministic Jev HTTP mock using synthetic fixtures only."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_answer(row, question, condition):
    kind = question['type']
    if kind == 'noul':
        target = 'yellow' if 'yellow' in condition.lower() else 'red'
        return {'type': 'noul', 'noul': 0.9 if row.get('color') == target else 0.1}
    options = list(question['criteria'])
    if kind == 'choice':
        selected = row.get('color', options[0])
        if selected not in options:
            selected = options[0]
        probabilities = {option: float(option == selected) for option in options}
        return {'type': 'choice', 'choice': selected, 'confidence': 1.0,
                'probabilities': probabilities}
    index = len(options) - 1 if row.get('temperature_c', 0) >= 30 else 0
    return {'type': 'score', 'score': float(index), 'confidence': 1.0,
            'legend': {str(i): option for i, option in enumerate(options)},
            'probabilities': {str(i): float(i == index) for i in range(len(options))}}


class MockAPI:
    """Close the server and its thread when the context exits."""

    def __init__(self, expected_model='mock/result', mode='success', expected_token=None,
                 reverse_first_pair=False, address=('127.0.0.1', 0)):
        self.expected_model = expected_model
        self.expected_token = expected_token
        self.authorized_requests = 0
        self.mode = mode
        self.reverse_first_pair = reverse_first_pair
        self.second_finished = threading.Event()
        self.requests = []
        self.completions = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    if owner.expected_token is not None:
                        if self.headers.get('Authorization') != 'Bearer ' + owner.expected_token:
                            self.send_error(401)
                            return
                        with owner.lock:
                            owner.authorized_requests += 1
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1000000:
                        self.send_error(400)
                        return
                    body = json.loads(self.rfile.read(length))
                    state = body['state']
                    if isinstance(state, str):
                        state = json.loads(state)
                    questions = body['questions']
                    kind = next(iter(questions.values()))['type']
                    with owner.lock:
                        request_index = len(owner.requests)
                        owner.requests.append(body)
                        owner.active += 1
                        owner.max_active = max(owner.max_active, owner.active)
                    try:
                        time.sleep({'noul': 0.08, 'choice': 0.01, 'score': 0.03}[kind])
                        answers = {}
                        for key in reversed(list(questions)):
                            index = int(key[1:])
                            answers[key] = make_answer(state['rows'][index], questions[key],
                                                       state['condition'])
                        if owner.mode == 'missing':
                            answers.pop(next(iter(answers)))
                        if owner.mode == 'nan':
                            answers[next(iter(answers))]['noul'] = float('nan')
                        result = {'model': owner.expected_model, 'answers': answers}
                        status = 200
                        if owner.mode == 'error':
                            status = 422
                            result = {'error': 'SENSITIVE_REMOTE_BODY'}
                        encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
                        if owner.reverse_first_pair and request_index == 0:
                            if not owner.second_finished.wait(timeout=1):
                                self.send_error(504)
                                return
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(encoded)))
                        self.end_headers()
                        self.wfile.write(encoded)
                        with owner.lock:
                            owner.completions.append(kind)
                        if request_index == 1:
                            owner.second_finished.set()
                    finally:
                        with owner.lock:
                            owner.active -= 1
                except (ValueError, KeyError, TypeError):
                    self.send_error(400)

        self.server = ThreadingHTTPServer(address, Handler)
        self.server.daemon_threads = True
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}/judge'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
