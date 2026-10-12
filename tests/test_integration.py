import json
import os
import unittest
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import importlib.util
import inspect
import shutil
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SyntheticFixtureTests(unittest.TestCase):
    def test_public_fixture_has_two_fixed_cases_per_primitive(self):
        path = ROOT / 'tests' / 'fixtures' / 'primitives.json'
        self.assertTrue(path.is_file(), '고정된 합성 fixture가 필요하다')
        fixture = json.loads(path.read_text(encoding='utf-8'))
        cases = fixture['cases']
        self.assertEqual([case['kind'] for case in cases], ['noul', 'noul', 'choice', 'choice', 'score', 'score'])
        self.assertEqual([case['row']['item'] for case in cases],
                         ['red apple', 'yellow banana', 'yellow banana', 'green pear', 'hot weather', 'cold weather'])
        self.assertEqual(len(cases), 6)
        self.assertTrue(all(case['question'].strip() for case in cases))
        for start in (0, 2, 4):
            self.assertEqual(cases[start]['question'], cases[start + 1]['question'])
            self.assertEqual(cases[start]['options'], cases[start + 1]['options'])
        self.assertEqual(cases[0]['options'], [])
        self.assertEqual(cases[2]['options'], ['red', 'yellow', 'green'])
        self.assertEqual(cases[4]['options'], ['cold', 'mild', 'hot'])


class InstallerTests(unittest.TestCase):
    def test_explicit_directory_generates_safe_worker_xml_and_sql(self):
        installer = ROOT / 'scripts' / 'install.py'
        self.assertTrue(installer.is_file(), '명시 경로 설치기를 제공해야 한다')
        with tempfile.TemporaryDirectory(prefix="jev space '") as temp:
            output = Path(temp) / 'isolated'
            command = [sys.executable, str(installer), '--output-dir', str(output),
                       '--endpoint', 'http://127.0.0.1:8765/judge', '--allow-http',
                       '--model', 'mock/request', '--expected-model', 'mock/result',
                       '--batch-size', '2', '--concurrency', '2',
                       '--python', sys.executable, '--package-root', str(ROOT)]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            config = ET.parse(output / 'jev_function.xml').getroot()
            function = config.find('function')
            self.assertEqual(function.findtext('name'), 'jev_eval')
            self.assertEqual(function.findtext('type'), 'executable_pool')
            self.assertEqual(function.findtext('return_type'), 'String')
            self.assertEqual(function.findtext('format'), 'JSONEachRow')
            self.assertEqual(function.findtext('send_chunk_header'), '1')
            self.assertEqual([arg.findtext('name') for arg in function.findall('argument')],
                             ['row_json', 'question', 'kind', 'options_json'])
            self.assertTrue(all(arg.findtext('type') == 'String'
                                for arg in function.findall('argument')))
            wrapper = output / 'jev-worker'
            self.assertTrue(wrapper.stat().st_mode & 0o111)
            fake_python = Path(temp) / 'print argv.py'
            fake_python.write_text('#!' + sys.executable + '\nimport json,sys; print(json.dumps(sys.argv))')
            fake_python.chmod(0o755)
            command[command.index(sys.executable, 1)] = str(fake_python)
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            executed = subprocess.run([str(wrapper)], capture_output=True, text=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)
            argv = json.loads(executed.stdout)
            self.assertEqual(argv[argv.index('--model') + 1], 'mock/request')
            self.assertEqual(argv[argv.index('--expected-model') + 1], 'mock/result')
            self.assertIn('--allow-http', argv)
            sql = (output / 'functions.sql').read_text()
            self.assertIn('CREATE FUNCTION IF NOT EXISTS jev_prob', sql)
            self.assertIn('CREATE FUNCTION IF NOT EXISTS jev_score_norm', sql)
            local_config = ET.parse(output / 'config.xml').getroot()
            self.assertEqual(local_config.findtext('user_scripts_path'), str(output.resolve()) + '/')
            self.assertEqual(local_config.findtext('user_defined_executable_functions_config'),
                             str(output.resolve() / 'jev_function.xml'))


    def test_explicit_credential_environment_name_is_forwarded_without_value(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'installed'
            command = [sys.executable, str(ROOT / 'scripts' / 'install.py'),
                       '--output-dir', str(output), '--endpoint', 'https://example.invalid/judge',
                       '--model', 'mock/request', '--expected-model', 'mock/result',
                       '--api-key-env', 'JEV_TEST_API_KEY']
            environment = dict(os.environ, JEV_TEST_API_KEY='harmless-fixture-token')
            completed = subprocess.run(command, capture_output=True, text=True, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            wrapper = (output / 'jev-worker').read_text()
            self.assertIn('--api-key-env JEV_TEST_API_KEY', wrapper)
            for path in output.iterdir():
                self.assertNotIn('harmless-fixture-token', path.read_text())
            completed = subprocess.run(command + ['--api-key-fd', '9'],
                                       capture_output=True, text=True, env=environment)
            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn('harmless-fixture-token', completed.stderr)


def load_mock():
    spec = importlib.util.spec_from_file_location('jev_mock_api', ROOT / 'tests' / 'mock_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MockAPITests(unittest.TestCase):
    def test_three_primitives_return_configured_model_and_row_index_answers(self):
        self.assertTrue((ROOT / 'tests' / 'mock_api.py').is_file(),
                        '결정론적 loopback mock이 필요하다')
        cases = json.loads((ROOT / 'tests' / 'fixtures' / 'primitives.json').read_text())['cases']
        for offset in (0, 2, 4):
            pair = cases[offset:offset + 2]
            kind = pair[0]['kind']
            questions = {}
            for i, case in enumerate(pair):
                question = {'type': kind, 'instructions': 'fixture'}
                if kind != 'noul':
                    question['criteria'] = (case['options'] if kind == 'score'
                                            else dict.fromkeys(case['options']))
                questions['r' + str(i)] = question
            body = {'model': 'mock/request',
                    'state': json.dumps({'condition': pair[0]['question'],
                                         'rows': [case['row'] for case in pair]}),
                    'questions': questions}
            with load_mock().MockAPI(expected_model='mock/result') as mock:
                request = Request(mock.endpoint, data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=2) as response:
                    result = json.load(response)
                self.assertEqual(result['model'], 'mock/result')
                self.assertEqual(list(result['answers']), ['r1', 'r0'])
                answers = result['answers']
                if kind == 'noul':
                    self.assertEqual([answers['r0']['noul'], answers['r1']['noul']], [0.9, 0.1])
                elif kind == 'choice':
                    self.assertEqual([answers['r0']['choice'], answers['r1']['choice']],
                                     ['yellow', 'green'])
                else:
                    self.assertEqual([answers['r0']['score'], answers['r1']['score']], [2, 0])
                    self.assertEqual(answers['r0']['legend'],
                                     {'0': 'cold', '1': 'mild', '2': 'hot'})
                self.assertEqual(len(mock.requests), 1)


    def test_mock_requires_bearer_when_fixture_token_is_configured(self):
        module = load_mock()
        self.assertIn('expected_token', inspect.signature(module.MockAPI).parameters)
        body = {'model': 'mock/request', 'state': json.dumps({'condition': 'red',
                'rows': [{'color': 'red'}]}), 'questions': {'r0': {'type': 'noul'}}}
        encoded = json.dumps(body).encode()
        with module.MockAPI(expected_token='harmless-fixture-token') as mock:
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(mock.endpoint, data=encoded), timeout=2)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            request = Request(mock.endpoint, data=encoded,
                              headers={'Authorization': 'Bearer harmless-fixture-token'})
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response)['answers']['r0']['noul'], 0.9)
            self.assertEqual(mock.authorized_requests, 1)


@unittest.skipUnless(shutil.which('clickhouse-local'), '실제 CH E2E는 개발 컨테이너에서 실행한다')
class ClickHouseE2ETests(unittest.TestCase):
    def run_query(self, mock, cases, select, block_size=6, credential=False):
        with tempfile.TemporaryDirectory(prefix='jev-e2e-') as temp:
            directory = Path(temp)
            installed = directory / 'installed'
            source = Path(os.environ.get('JEV_PACKAGE_ROOT', str(ROOT))).resolve()
            command = [sys.executable, str(ROOT / 'scripts' / 'install.py'),
                       '--output-dir', str(installed), '--package-root', str(source),
                       '--python', sys.executable, '--endpoint', mock.endpoint,
                       '--allow-http', '--model', 'mock/request',
                       '--expected-model', 'mock/result', '--batch-size', '2',
                       '--concurrency', '2', '--deadline-seconds', '3']
            if credential:
                command += ['--api-key-env', 'JEV_FIXTURE_TOKEN']
            installation = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(installation.returncode, 0, '독립 경로 설치 실패')
            input_file = directory / 'rows.jsonl'
            with input_file.open('w', encoding='utf-8') as stream:
                for index, case in enumerate(cases):
                    payload = dict(id=index, row_json=json.dumps(case['row'], ensure_ascii=False),
                                   question=case['question'], kind=case['kind'],
                                   options_json=json.dumps(case['options'], ensure_ascii=False))
                    stream.write(json.dumps(payload, ensure_ascii=False) + '\n')
            query_file = directory / 'query.sql'
            query_file.write_text((installed / 'functions.sql').read_text() + '\n' + select
                                 + f' SETTINGS max_block_size={block_size}, max_threads=1 FORMAT JSONEachRow;')
            environment = os.environ.copy()
            if credential:
                environment['JEV_FIXTURE_TOKEN'] = 'harmless-fixture-token'
            command = ['clickhouse-local', '--config-file', str(installed / 'config.xml'),
                       '--table', 'rows', '--structure',
                       'id UInt64, row_json String, question String, kind String, options_json String',
                       '--input-format', 'JSONEachRow', '--file', str(input_file),
                       '--queries-file', str(query_file)]
            completed = subprocess.run(command, capture_output=True, text=True,
                                       env=environment, timeout=15)
            return completed

    def test_sql_three_primitives_threshold_and_normalized_score(self):
        self.assertTrue((ROOT / 'scripts' / 'offline_e2e.py').is_file(),
                        '컨테이너의 offline E2E 진입점이 필요하다')
        cases = json.loads((ROOT / 'tests' / 'fixtures' / 'primitives.json').read_text())['cases']
        with load_mock().MockAPI() as mock:
            completed = self.run_query(mock, cases,
                'SELECT id, jev_eval(row_json,question,kind,options_json) AS answer FROM rows')
            self.assertEqual(completed.returncode, 0,
                             f'CH primitive 실행 실패: exit={completed.returncode}')
            outputs = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual([row['id'] for row in outputs], list(range(6)))
            answers = [json.loads(row['answer']) for row in outputs]
            self.assertEqual([answers[i]['noul'] for i in (0, 1)], [0.9, 0.1])
            self.assertEqual([answers[i]['choice'] for i in (2, 3)], ['yellow', 'green'])
            self.assertEqual([answers[i]['score'] for i in (4, 5)], [2, 0])
            self.assertEqual(len(mock.requests), 3)
        with load_mock().MockAPI() as mock:
            completed = self.run_query(mock, cases[:2],
                'SELECT id, jev_prob(row_json,question) AS probability, '
                'jev(row_json,question,0.5) AS accepted FROM rows')
            self.assertEqual(completed.returncode, 0, 'CH noul wrapper 실행 실패')
            outputs = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual([row['probability'] for row in outputs], [0.9, 0.1])
            self.assertEqual([row['accepted'] for row in outputs], [1, 0])
        with load_mock().MockAPI() as mock:
            completed = self.run_query(mock, cases[2:4],
                'SELECT id, jev_choice(row_json,question,options_json) AS selected FROM rows')
            self.assertEqual(completed.returncode, 0, 'CH choice wrapper 실행 실패')
            self.assertEqual([json.loads(line)['selected'] for line in completed.stdout.splitlines()],
                             ['yellow', 'green'])
        with load_mock().MockAPI() as mock:
            completed = self.run_query(mock, cases[4:],
                'SELECT id, jev_score(row_json,question,options_json) AS score, '
                'jev_score_norm(row_json,question,options_json) AS normalized FROM rows')
            self.assertEqual(completed.returncode, 0, 'CH score wrapper 실행 실패')
            outputs = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual([row['score'] for row in outputs], [2, 0])
            self.assertEqual([row['normalized'] for row in outputs], [1, 0])


    def test_mixed_prompts_duplicates_unicode_and_cross_blocks_preserve_order(self):
        fixture = ROOT / 'tests' / 'fixtures' / 'order.json'
        self.assertTrue(fixture.is_file(), '순서 복원용 공개 적대 fixture가 필요하다')
        cases = json.loads(fixture.read_text())['cases']
        expected = [case['expected'] for case in cases]
        for block_size in (9, 3):
            with self.subTest(block_size=block_size), load_mock().MockAPI(reverse_first_pair=True) as mock:
                completed = self.run_query(mock, cases,
                    'SELECT id, jev_eval(row_json,question,kind,options_json) AS answer FROM rows',
                    block_size=block_size)
                self.assertEqual(completed.returncode, 0,
                                 f'CH 순서 복원 실패: exit={completed.returncode}')
                outputs = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual([row['id'] for row in outputs], list(range(len(cases))))
                answers = [json.loads(row['answer']) for row in outputs]
                actual = [answer[case['kind']] for answer, case in zip(answers, cases)]
                self.assertEqual(actual, expected)
                states = [json.loads(body['state']) for body in mock.requests]
                self.assertEqual(sum(len(state['rows']) for state in states), len(cases))
                self.assertTrue(all(len(state['rows']) <= 2 for state in states))
                sent = [row for state in states for row in state['rows']]
                self.assertIn({'item': 'red apple', 'color': 'red',
                               'label': '서울', 'blank': ''}, sent)
                second_kind = next(iter(mock.requests[1]['questions'].values()))['type']
                self.assertEqual(mock.completions[0], second_kind)
                self.assertNotEqual([next(iter(body['questions'].values()))['type']
                                     for body in mock.requests], mock.completions)


    def test_missing_nan_and_api_errors_fail_whole_block_without_output(self):
        fixture = ROOT / 'tests' / 'fixtures' / 'failures.json'
        self.assertTrue(fixture.is_file(), '오류 블록 검증용 공개 fixture가 필요하다')
        modes = json.loads(fixture.read_text())['modes']
        cases = json.loads((ROOT / 'tests' / 'fixtures' / 'primitives.json').read_text())['cases'][:2]
        cases[0]['row']['marker'] = 'SENSITIVE_INPUT'
        for mode in modes:
            with self.subTest(mode=mode), load_mock().MockAPI(mode=mode) as mock:
                completed = self.run_query(mock, cases,
                    'SELECT id, jev_eval(row_json,question,kind,options_json) AS answer FROM rows',
                    block_size=2)
                self.assertNotEqual(completed.returncode, 0, '오류 블록은 CH 쿼리를 실패시켜야 한다')
                self.assertFalse(completed.stdout, '실패 블록은 부분 결과를 출력할 수 없다')
                self.assertFalse('SENSITIVE_INPUT' in completed.stderr, '입력 본문이 오류에 노출됨')
                self.assertFalse('SENSITIVE_REMOTE_BODY' in completed.stderr, '원격 본문이 오류에 노출됨')
                self.assertEqual(len(mock.requests), 1)


    def test_worker_uses_explicit_environment_fixture_token_for_bearer(self):
        cases = json.loads((ROOT / 'tests' / 'fixtures' / 'primitives.json').read_text())['cases']
        with load_mock().MockAPI(expected_token='harmless-fixture-token') as mock:
            rejected = self.run_query(mock, cases,
                'SELECT id, jev_eval(row_json,question,kind,options_json) AS answer FROM rows')
            self.assertNotEqual(rejected.returncode, 0, '인증 없는 worker 요청은 실패해야 한다')
            self.assertFalse(rejected.stdout, '인증 실패 블록은 stdout 결과가 없어야 한다')
            self.assertEqual(mock.authorized_requests, 0)
            accepted = self.run_query(mock, cases,
                'SELECT id, jev_eval(row_json,question,kind,options_json) AS answer FROM rows',
                credential=True)
            self.assertEqual(accepted.returncode, 0, '명시 환경변수 인증 경로 실패')
            outputs = [json.loads(line) for line in accepted.stdout.splitlines()]
            self.assertEqual([row['id'] for row in outputs], list(range(6)))
            self.assertEqual(mock.authorized_requests, 3)
            self.assertEqual(len(mock.requests), 3)
            self.assertFalse('harmless-fixture-token' in accepted.stderr,
                             'credential 값은 진단에 포함될 수 없다')


if __name__ == '__main__':
    unittest.main()
