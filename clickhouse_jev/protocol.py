"""Explicit data contracts for Jev requests and responses."""
from dataclasses import dataclass
import json
import math

_ANSWER_FIELDS = {
    'noul': {'type', 'noul'},
    'choice': {'type', 'choice', 'confidence', 'probabilities'},
    'score': {'type', 'score', 'confidence', 'legend', 'probabilities'},
}


class ContractError(ValueError):
    """Contract error that omits input and remote response bodies."""


@dataclass(frozen=True)
class RowInput:
    position: int
    row: dict
    question: str
    kind: str
    options: tuple[str, ...]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('invalid_json')
        result[key] = value
    return result


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ContractError('invalid_json')
    return value


def _reject_constant(text):
    raise ContractError('invalid_json')


def _decode_json(text):
    try:
        return json.loads(text, object_pairs_hook=_unique_object,
                          parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise ContractError('invalid_json') from None


def parse_input(payload: dict, position: int, max_row_chars: int) -> RowInput:
    if (not isinstance(payload, dict)
            or set(payload) != {'row_json', 'question', 'kind', 'options_json'}
            or any(not isinstance(value, str) for value in payload.values())):
        raise ContractError('invalid_input_fields')
    if not payload['question'].strip() or payload['kind'] not in ('noul', 'choice', 'score'):
        raise ContractError('invalid_judgment')
    if (type(position) is not int or position < 0
            or type(max_row_chars) is not int or max_row_chars < 1):
        raise ContractError('invalid_input_limits')
    if len(payload['row_json']) > max_row_chars:
        raise ContractError('row_too_large')
    row = _decode_json(payload['row_json'])
    if not isinstance(row, dict):
        raise ContractError('invalid_row')
    options = _decode_json(payload['options_json'])
    if not isinstance(options, list) or any(not isinstance(value, str) for value in options):
        raise ContractError('invalid_options')
    if payload['kind'] == 'noul' and options:
        raise ContractError('invalid_options')
    if payload['kind'] != 'noul' and (len(options) < 2 or len(set(options)) != len(options)
                                       or any(not value.strip() for value in options)):
        raise ContractError('invalid_options')
    return RowInput(position, row, payload['question'],
                    payload['kind'], tuple(options))


def _check_batch(rows):
    if not isinstance(rows, list) or not rows:
        raise ContractError('invalid_batch')
    positions = set()
    contract = None
    for row in rows:
        if (not isinstance(row, RowInput) or not isinstance(row.row, dict)
                or not isinstance(row.options, tuple)):
            raise ContractError('invalid_batch')
        try:
            encoded = json.dumps(row.row, ensure_ascii=False, allow_nan=False)
            parsed = parse_input({'row_json': encoded, 'question': row.question,
                                  'kind': row.kind, 'options_json': json.dumps(list(row.options))},
                                 row.position, max(1, len(encoded)))
        except (ValueError, TypeError, RecursionError):
            raise ContractError('invalid_batch') from None
        if parsed.row != row.row or row.position in positions:
            raise ContractError('invalid_batch')
        positions.add(row.position)
        current = (row.question, row.kind, row.options)
        if contract is not None and current != contract:
            raise ContractError('mixed_batch')
        contract = current


def build_request(model: str, rows: list[RowInput], state_format: str = 'string') -> dict:
    if not isinstance(model, str) or not model.strip():
        raise ContractError('invalid_model')
    if state_format not in ('string', 'object'):
        raise ContractError('invalid_state_format')
    _check_batch(rows)
    first = rows[0]
    state = {'condition': first.question, 'rows': [row.row for row in rows]}
    questions = {}
    for index, row in enumerate(rows):
        ref = f'rows[{index}]'
        if row.kind == 'noul':
            question = {'type': 'noul',
                        'instructions': f'Does the record `{ref}` satisfy the condition stated in `condition`?'}
        elif row.kind == 'score':
            question = {'type': 'score', 'instructions': f'Rate the record `{ref}`: {row.question}',
                        'criteria': list(row.options)}
        else:
            question = {'type': 'choice', 'instructions': f'For the record `{ref}`: {row.question}',
                        'criteria': dict.fromkeys(row.options)}
        questions[f'r{index}'] = question
    wire_state = state if state_format == 'object' else json.dumps(state, ensure_ascii=False, allow_nan=False)
    return {'model': model, 'state': wire_state,
            'questions': questions}


def _number(value, lower, upper):
    if (type(value) not in (int, float) or not lower <= value <= upper
            or not math.isfinite(value)):
        raise ContractError('invalid_answer_number')
    return value


def _close(actual, expected):
    # Account for binary floating-point representation error at the boundary.
    return abs(actual - expected) <= 0.02 + 1e-12


def _distribution(probabilities, keys):
    if not isinstance(probabilities, dict) or set(probabilities) != set(keys):
        raise ContractError('invalid_probability_keys')
    values = [_number(probabilities[key], 0, 1) for key in keys]
    if not _close(math.fsum(values), 1):
        raise ContractError('invalid_probability_sum')


def validate_response(data: dict, rows: list[RowInput], expected_model: str) -> list[dict]:
    _check_batch(rows)
    if (not isinstance(expected_model, str) or not expected_model.strip()
            or not isinstance(data, dict) or data.get('model') != expected_model):
        raise ContractError('response_model_mismatch')
    answers = data.get('answers')
    if not isinstance(answers, dict) or set(answers) != {f'r{i}' for i in range(len(rows))}:
        raise ContractError('invalid_answer_keys')
    result = []
    for index, row in enumerate(rows):
        answer = answers[f'r{index}']
        if (not isinstance(answer, dict) or set(answer) != _ANSWER_FIELDS[row.kind]
                or answer.get('type') != row.kind):
            raise ContractError('invalid_answer_shape')
        if row.kind == 'noul':
            _number(answer['noul'], 0, 1)
        else:
            _number(answer['confidence'], 0, 1)
            keys = row.options if row.kind == 'choice' else tuple(str(i) for i in range(len(row.options)))
            _distribution(answer['probabilities'], keys)
            if row.kind == 'choice':
                selected = answer['choice']
                # Treat a probability difference from the maximum of at most 1e-12 as a tie.
                if (not isinstance(selected, str) or selected not in row.options
                        or max(answer['probabilities'].values()) - answer['probabilities'][selected] > 1e-12):
                    raise ContractError('invalid_choice')
            else:
                legend = {str(i): option for i, option in enumerate(row.options)}
                if not isinstance(answer['legend'], dict) or answer['legend'] != legend:
                    raise ContractError('invalid_score_legend')
                score = _number(answer['score'], 0, len(row.options) - 1)
                expected = math.fsum(i * answer['probabilities'][str(i)] for i in range(len(row.options)))
                if not _close(score, expected):
                    raise ContractError('invalid_score_expectation')
        result.append(answer)
    return result
