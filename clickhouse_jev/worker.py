"""Evaluate blocks and process JSONEachRow streams."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import math
import time

from .protocol import ContractError, build_request, parse_input, validate_response


def evaluate_block(payloads, config, transport):
    deadline = time.monotonic() + config.deadline_seconds

    def remaining_seconds():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ContractError('worker_deadline')
        return seconds

    def execute(body):
        return transport(body, remaining_seconds())

    if len(payloads) > config.max_block_rows:
        raise ContractError('worker_block_limit')
    groups = {}
    for position, payload in enumerate(payloads):
        try:
            row = parse_input(payload, position, config.max_row_chars)
        except Exception:
            raise ContractError('worker_input_invalid') from None
        groups.setdefault((row.question, row.kind, row.options), []).append(row)
    batches = [rows[start:start + config.batch_size] for rows in groups.values()
               for start in range(0, len(rows), config.batch_size)]
    prepared = []
    for rows in batches:
        try:
            body = build_request(config.model, rows, config.state_format)
            size = len(json.dumps(body, ensure_ascii=False, separators=(',', ':'),
                                  allow_nan=False).encode('utf-8'))
        except Exception:
            raise ContractError('worker_request_invalid') from None
        if size > config.max_request_bytes:
            raise ContractError('worker_request_limit')
        prepared.append((rows, body))
    answers = [None] * len(payloads)
    pool = ThreadPoolExecutor(max_workers=config.concurrency)
    futures = {}
    try:
        remaining = iter(prepared)
        while True:
            while len(futures) < config.concurrency:
                item = next(remaining, None)
                if item is None:
                    break
                rows, body = item
                remaining_seconds()
                futures[pool.submit(execute, body)] = rows
            if not futures:
                break
            done, _ = wait(futures, timeout=remaining_seconds(), return_when=FIRST_COMPLETED)
            if not done:
                raise ContractError('worker_deadline')
            for future in done:
                rows = futures.pop(future)
                for row, answer in zip(rows, validate_response(future.result(), rows, config.expected_model)):
                    answers[row.position] = answer
                remaining_seconds()
    except Exception:
        raise ContractError('worker_block_failed') from None
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return answers


def serve(inp, out, config, transport):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ContractError('worker_json_invalid')
            value[key] = item
        return value

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ContractError('worker_json_invalid')
        return number

    def invalid_constant(value):
        raise ContractError('worker_json_invalid')

    def read_line(limit):
        try:
            line = inp.readline(limit + 1)
        except Exception:
            raise ContractError('worker_stream_read') from None
        if len(line) > limit:
            raise ContractError('worker_line_limit')
        return line

    # Allow space for Unicode and quote escaping, including the outer JSON envelope.
    line_limit = 6 * (config.max_request_bytes + config.max_row_chars) + 1024
    header_limit = len(str(config.max_block_rows)) + 2
    while True:
        header = read_line(header_limit)
        if header == '':
            return
        text = header.removesuffix('\n').removesuffix('\r')
        if not header.endswith('\n') or not text.isascii() or not text.isdecimal():
            raise ContractError('worker_header_invalid')
        count = int(text)
        if count > config.max_block_rows:
            raise ContractError('worker_block_limit')
        if count == 0:
            continue
        payloads = []
        for _ in range(count):
            line = read_line(line_limit)
            if line == '' or not line.strip():
                raise ContractError('worker_block_truncated')
            try:
                payload = json.loads(line, object_pairs_hook=unique_object,
                                     parse_constant=invalid_constant, parse_float=finite_float)
            except Exception:
                raise ContractError('worker_json_invalid') from None
            if not isinstance(payload, dict):
                raise ContractError('worker_input_invalid')
            payloads.append(payload)
        answers = evaluate_block(payloads, config, transport)
        output = ''.join(json.dumps({'result': json.dumps(answer, ensure_ascii=False, allow_nan=False)},
                                    ensure_ascii=False, allow_nan=False) + '\n' for answer in answers)
        out.write(output)
        out.flush()
