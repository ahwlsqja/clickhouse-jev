"""Verify public input and response contracts without external connections."""
from dataclasses import FrozenInstanceError
import json
import unittest

from clickhouse_jev.protocol import ContractError, RowInput, parse_input, build_request, validate_response


def payload(**changes):
    value = {'row_json': '{"name":"caf\u00e9","blank":"","nested":[null,true,2]}',
             'question': '  Is this a cafe?  ', 'kind': 'noul', 'options_json': '[]'}
    value.update(changes)
    return value


def input_rows(kind='noul', options=None):
    if options is None:
        options = [] if kind == 'noul' else ['low', 'high']
    return [parse_input(payload(kind=kind, options_json=json.dumps(options)), 0, 1000)]


def response(answer):
    return {'model': 'fixture', 'answers': {'r0': answer}}


class ProtocolTests(unittest.TestCase):
    def assert_safe_rejection(self, call):
        with self.assertRaises(ContractError) as caught:
            call()
        self.assertNotIn('SENSITIVE', str(caught.exception))

    def test_p01_preserves_unicode_blank_values_position_and_frozen_fields(self):
        parsed = parse_input(payload(), 7, 1000)
        self.assertIsNotNone(parsed, 'Input must be converted into RowInput')
        self.assertEqual(parsed.row, {'name': 'caf\u00e9', 'blank': '', 'nested': [None, True, 2]})
        self.assertEqual((parsed.position, parsed.question, parsed.kind, parsed.options),
                         (7, '  Is this a cafe?  ', 'noul', ()))
        with self.assertRaises(FrozenInstanceError):
            parsed.position = 8

    def test_p02_requires_exact_string_payload_fields(self):
        for value in [payload(extra='SENSITIVE'), {k: v for k, v in payload().items() if k != 'kind'},
                      None, [], payload(question=None), payload(row_json={}),
                      payload(kind=1), payload(options_json=[])]:
            self.assert_safe_rejection(lambda: parse_input(value, 0, 1000))

    def test_p03_requires_nonblank_question_and_known_kind(self):
        for changes in [{'question': ''}, {'question': ' \n\t'}, {'kind': 'Noul'},
                        {'kind': 'SENSITIVE'}, {'kind': ''}]:
            self.assert_safe_rejection(lambda: parse_input(payload(**changes), 0, 1000))
        for kind in ['noul', 'choice', 'score']:
            options = '[]' if kind == 'noul' else '["a","b"]'
            self.assertEqual(parse_input(payload(kind=kind, options_json=options), 0, 1000).kind, kind)

    def test_p04_requires_strict_finite_json_object(self):
        for text in ['[]', 'null', '7', '"SENSITIVE"', '{SENSITIVE',
                     '{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}',
                     '{"x":NaN}', '{"x":Infinity}', '{"x":[-Infinity]}', '{"x":1e309}']:
            self.assert_safe_rejection(lambda: parse_input(payload(row_json=text), 0, 1000))
        self.assertEqual(parse_input(payload(row_json='{}'), 0, 1000).row, {})

    def test_p05_bounds_position_and_raw_row_characters(self):
        value = payload()
        length = len(value['row_json'])
        self.assert_safe_rejection(lambda: parse_input(value, 0, length - 1))
        self.assertEqual(parse_input(value, 0, length).position, 0)
        for position in [-1, True, 0.5, '0', None]:
            self.assert_safe_rejection(lambda: parse_input(value, position, 1000))
        for limit in [0, -1, True, 10.0, '1000', None]:
            self.assert_safe_rejection(lambda: parse_input(value, 0, limit))

    def test_p06_requires_string_options_array_and_empty_noul_options(self):
        for text in ['["SENSITIVE"]', 'null', '{}', '"ab"', '[1]', '[null]',
                     '[true]', '[NaN]', '[', '']:
            self.assert_safe_rejection(lambda: parse_input(payload(options_json=text), 0, 1000))
        self.assertEqual(parse_input(payload(options_json=' [ ] '), 0, 1000).options, ())

    def test_p07_requires_unique_nonblank_choice_and_score_options(self):
        for kind in ['choice', 'score']:
            for text in ['[]', '["one"]', '["a","a"]', '["a",""]', json.dumps(['a', ' \n\t'])]:
                self.assert_safe_rejection(lambda: parse_input(payload(kind=kind, options_json=text), 0, 1000))
            parsed = parse_input(payload(kind=kind, options_json='["  caf\u00e9  ","plain"]'), 0, 1000)
            self.assertEqual(parsed.options, ('  caf\u00e9  ', 'plain'))

    def test_p08_builds_ordered_questions_and_json_string_state(self):
        for kind in ['noul', 'choice', 'score']:
            opts = [] if kind == 'noul' else [' caf\u00e9 ', 'plain']
            rows = [parse_input(payload(kind=kind, options_json=json.dumps(opts)), 7, 1000),
                    parse_input(payload(row_json='{"blank":""}', kind=kind,
                                        options_json=json.dumps(opts)), 2, 1000)]
            request = build_request('typesafe/fixture', rows)
            self.assertEqual(set(request), {'model', 'state', 'questions'})
            self.assertEqual(request['model'], 'typesafe/fixture')
            self.assertIsInstance(request['state'], str)
            self.assertIn('caf\u00e9', request['state'])
            self.assertEqual(json.loads(request['state']),
                             {'condition': rows[0].question, 'rows': [row.row for row in rows]})
            self.assertEqual(list(request['questions']), ['r0', 'r1'])
            for index in range(2):
                question = request['questions'][f'r{index}']
                self.assertEqual(question['type'], kind)
                self.assertIn(f'rows[{index}]', question['instructions'])
                if kind == 'noul':
                    self.assertEqual(set(question), {'type', 'instructions'})
                    self.assertIn('condition', question['instructions'])
                else:
                    self.assertIn(rows[0].question, question['instructions'])
                    self.assertEqual(question['criteria'], opts if kind == 'score' else dict.fromkeys(opts))

    def test_p09_supports_explicit_object_state_without_changing_content(self):
        rows = [parse_input(payload(), 0, 1000)]
        string = build_request('fixture', rows)
        obj = build_request('fixture', rows, state_format='object')
        self.assertIsInstance(obj['state'], dict)
        self.assertEqual(obj['state'], json.loads(string['state']))
        self.assertEqual(obj['questions'], string['questions'])

    def test_p10_rejects_invalid_models_formats_and_batch_contracts(self):
        row = parse_input(payload(), 0, 1000)
        for model in ['', ' \t', None, 7]:
            self.assert_safe_rejection(lambda: build_request(model, [row]))
        for fmt in ['', 'array', None, {}]:
            self.assert_safe_rejection(lambda: build_request('fixture', [row], fmt))
        altered = [RowInput(1, {}, 'different', 'noul', ()),
                   RowInput(1, {}, row.question, 'choice', ('a', 'b')),
                   RowInput(1, {}, row.question, 'noul', ('a',)),
                   RowInput(1, {'x': float('nan')}, row.question, 'noul', ()),
                   RowInput(1, {1: 'value'}, row.question, 'noul', ()),
                   RowInput(1, {'x': (1, 2)}, row.question, 'noul', ()),
                   RowInput(-1, {}, row.question, 'noul', ()),
                   RowInput(1, [], row.question, 'noul', ())]
        for batch in [[], None, (row,), [{}], [row, row], *[[row, bad] for bad in altered]]:
            self.assert_safe_rejection(lambda: build_request('fixture', batch))
        choice = parse_input(payload(kind='choice', options_json='["a","b"]'), 0, 1000)
        other = parse_input(payload(kind='choice', options_json='["b","a"]'), 1, 1000)
        self.assert_safe_rejection(lambda: build_request('fixture', [choice, other]))

    def test_p11_requires_exact_model_and_answer_keys_in_input_order(self):
        rows = input_rows()
        answer = {'type': 'noul', 'noul': 0.7}
        for data in [{'model': 'SENSITIVE', 'answers': {'r0': answer}}, None, [],
                     {'answers': {'r0': answer}}, {'model': 1, 'answers': {'r0': answer}},
                     {'model': 'fixture'}, {'model': 'fixture', 'answers': None},
                     {'model': 'fixture', 'answers': []},
                     {'model': 'fixture', 'answers': {}},
                     {'model': 'fixture', 'answers': {'r0': answer, 'r1': answer}},
                     {'model': 'fixture', 'answers': {'R0': answer}}]:
            self.assert_safe_rejection(lambda: validate_response(data, rows, 'fixture'))
        for expected in ['', ' ', None, 1]:
            self.assert_safe_rejection(lambda: validate_response(response(answer), rows, expected))
        self.assert_safe_rejection(lambda: validate_response(response(answer), [], 'fixture'))
        row = rows[0]
        pair = [RowInput(9, row.row, row.question, row.kind, row.options),
                RowInput(4, row.row, row.question, row.kind, row.options)]
        second = {'type': 'noul', 'noul': 0.2}
        data = {'model': 'fixture', 'answers': {'r1': second, 'r0': answer}, 'usage': {}}
        self.assertEqual(validate_response(data, pair, 'fixture'), [answer, second])

    def test_p12_requires_exact_primitive_type_and_answer_fields(self):
        valid = {
            'noul': {'type': 'noul', 'noul': 0.7},
            'choice': {'type': 'choice', 'choice': 'high', 'confidence': 0.6,
                       'probabilities': {'low': 0.1, 'high': 0.9}},
            'score': {'type': 'score', 'score': 0.75, 'confidence': 0.4,
                      'legend': {'0': 'low', '1': 'high'}, 'probabilities': {'0': 0.25, '1': 0.75}},
        }
        for kind, answer in valid.items():
            rows = input_rows(kind)
            invalid = [dict(answer, extra='SENSITIVE'), dict(answer, type='wrong'),
                       dict(answer, type=None), [], None]
            invalid += [{k: v for k, v in answer.items() if k != missing} for missing in answer]
            for bad in invalid:
                self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))
            data = response(answer)
            before = json.loads(json.dumps(data))
            self.assertEqual(validate_response(data, rows, 'fixture'), [answer])
            self.assertEqual(data, before)

    def test_p13_requires_finite_unit_noul_and_confidence_numbers(self):
        fixtures = [
            ('noul', 'noul', {'type': 'noul', 'noul': 0.7}),
            ('choice', 'confidence', {'type': 'choice', 'choice': 'high', 'confidence': 0.6,
                                     'probabilities': {'low': 0.1, 'high': 0.9}}),
            ('score', 'confidence', {'type': 'score', 'score': 0.75, 'confidence': 0.4,
                                    'legend': {'0': 'low', '1': 'high'},
                                    'probabilities': {'0': 0.25, '1': 0.75}}),
        ]
        for kind, field, answer in fixtures:
            rows = input_rows(kind)
            for value in [True, False, '0.7', None, [], float('nan'), float('inf'),
                          -float('inf'), -0.001, 1.001, 10 ** 400]:
                bad = dict(answer, **{field: value})
                self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))
            for value in [0, 0.5, 1]:
                good = dict(answer, **{field: value})
                self.assertEqual(validate_response(response(good), rows, 'fixture'), [good])

    def test_p14_requires_exact_finite_probability_distribution_and_sum(self):
        for kind in ['choice', 'score']:
            rows = input_rows(kind)
            keys = ['low', 'high'] if kind == 'choice' else ['0', '1']
            answer = {'type': kind, 'confidence': 0.6, 'probabilities': dict(zip(keys, [0.1, 0.9]))}
            if kind == 'choice':
                answer['choice'] = 'high'
            else:
                answer.update(score=0.9, legend={'0': 'low', '1': 'high'})
            invalid = [[], None, {keys[0]: 1.0}, dict(zip(keys + ['extra'], [0.1, 0.9, 0])),
                       dict(zip(keys, [0.1, 0.87])), dict(zip(keys, [0.1, 0.93]))]
            for value in [True, '0.1', None, float('nan'), float('inf'), -0.1, 1.1, 10 ** 400]:
                invalid.append(dict(zip(keys, [value, 0.9])))
            for probabilities in invalid:
                bad = dict(answer, probabilities=probabilities)
                self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))
            for value in [0.88, 0.9, 0.92]:
                good = dict(answer, probabilities=dict(zip(keys, [0.1, value])))
                if kind == 'score':
                    good['score'] = value
                self.assertEqual(validate_response(response(good), rows, 'fixture'), [good])

    def test_p15_requires_choice_argmax_but_not_confidence_equality(self):
        rows = input_rows('choice')
        answer = {'type': 'choice', 'choice': 'high', 'confidence': 0.6,
                  'probabilities': {'low': 0.1, 'high': 0.9}}
        for selected in ['low', 'SENSITIVE', '', None, True, []]:
            bad = dict(answer, choice=selected)
            self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))
        self.assertEqual(validate_response(response(answer), rows, 'fixture'), [answer])
        for selected in ['low', 'high']:
            tied = dict(answer, choice=selected, probabilities={'low': 0.5, 'high': 0.5})
            self.assertEqual(validate_response(response(tied), rows, 'fixture'), [tied])
        for probability in [0.49, 0.499999999998]:
            bad = dict(answer, choice='low', probabilities={'low': probability, 'high': 0.5})
            self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))

    def test_p18_choice_preserves_answers_with_only_numeric_roundoff(self):
        rows = input_rows('choice')
        for probability in [0.49999999999999994, 0.4999999999995]:
            answer = {'type': 'choice', 'choice': 'low', 'confidence': 0.5,
                      'probabilities': {'low': probability, 'high': 0.5}}
            self.assertEqual(validate_response(response(answer), rows, 'fixture'), [answer])

    def test_p16_requires_exact_ordered_score_legend(self):
        rows = input_rows('score', ['low', 'mid', 'high'])
        answer = {'type': 'score', 'score': 1.4, 'confidence': 0.3,
                  'legend': {'0': 'low', '1': 'mid', '2': 'high'},
                  'probabilities': {'0': 0.1, '1': 0.4, '2': 0.5}}
        for legend in [None, [], ['low', 'mid', 'high'], {'0': 'low', '1': 'mid'},
                       {'0': 'high', '1': 'mid', '2': 'low'},
                       {'0': 'low', '1': 'mid', '2': 'high', '3': 'SENSITIVE'},
                       {0: 'low', 1: 'mid', 2: 'high'}, {'0': 'low', '1': 1, '2': 'high'}]:
            bad = dict(answer, legend=legend)
            self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))
        self.assertEqual(validate_response(response(answer), rows, 'fixture'), [answer])

    def test_p17_requires_finite_bounded_score_matching_expected_index(self):
        rows = input_rows('score', ['low', 'mid', 'high'])
        answer = {'type': 'score', 'score': 1.4, 'confidence': 0.3,
                  'legend': {'0': 'low', '1': 'mid', '2': 'high'},
                  'probabilities': {'0': 0.1, '1': 0.4, '2': 0.5}}
        for score in [0.7, 1.3799, 1.4201, -0.01, 2.01, True, '1.4', None, [],
                      float('nan'), float('inf'), -float('inf'), 10 ** 400]:
            bad = dict(answer, score=score)
            self.assert_safe_rejection(lambda: validate_response(response(bad), rows, 'fixture'))
        for score in [1.38, 1.4, 1.42]:
            good = dict(answer, score=score)
            self.assertEqual(validate_response(response(good), rows, 'fixture'), [good])
        for score, probabilities in [(0, {'0': 1, '1': 0, '2': 0}),
                                     (2, {'0': 0, '1': 0, '2': 1})]:
            good = dict(answer, score=score, probabilities=probabilities)
            self.assertEqual(validate_response(response(good), rows, 'fixture'), [good])
