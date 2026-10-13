"""Acceptance contract for the disposable, real-server demo.

Create only documented synthetic fixtures in a dedicated demo database.
Check filtering, classification, scoring, and normalized scoring through the
server's executable UDF. Print the returned results and exit nonzero on setup,
query, or result mismatch. No external model or credential is required.
"""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ['clickhouse-client', '--host', 'clickhouse', '--user', 'demo', '--password', 'demo']
SETTINGS = (' LIMIT 10 SETTINGS max_block_size=3, max_threads=1, max_rows_to_read=10, '
            'max_execution_time=10, timeout_before_checking_execution_speed=0, '
            'max_memory_usage=268435456 FORMAT JSONEachRow')


def execute(sql, *, multiquery=False):
    command = CLIENT + (['--multiquery'] if multiquery else [])
    result = subprocess.run(command, input=sql, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('ClickHouse query failed (exit %d)' % result.returncode)
    return result.stdout


def check(label, sql, expected):
    actual = [json.loads(line) for line in execute(sql + SETTINGS).splitlines() if line.strip()]
    if actual != expected:
        raise RuntimeError('%s returned unexpected results' % label)
    print('%s: %s' % (label, json.dumps(actual, ensure_ascii=True)), flush=True)


def main():
    try:
        execute((ROOT / 'sql/functions.sql').read_text() + '\n'
                + (ROOT / 'demo/fixtures.sql').read_text(), multiquery=True)
        check('Filter', "SELECT id, item FROM jev_demo.items WHERE jev("
              "toJSONString(map('color', color)), 'Is the item red?', 0.5) ORDER BY id",
              [{'id': 1, 'item': 'red apple'}])
        check('Classification', "SELECT id, jev_choice(toJSONString(map('color', color)), "
              "'Choose the item color.', toJSONString(['red', 'yellow', 'green'])) AS color "
              "FROM jev_demo.items ORDER BY id",
              [{'id': 1, 'color': 'red'}, {'id': 2, 'color': 'yellow'}, {'id': 3, 'color': 'green'}])
        score_args = ("toJSONString(map('temperature_c', temperature_c)), "
                      "'Rate the temperature.', toJSONString(['cold', 'mild', 'hot'])")
        check('Scoring', 'SELECT id, jev_score(%s) AS score, jev_score_norm(%s) AS normalized '
              'FROM jev_demo.items ORDER BY id' % (score_args, score_args),
              [{'id': 1, 'score': 0, 'normalized': 0},
               {'id': 2, 'score': 2, 'normalized': 1},
               {'id': 3, 'score': 0, 'normalized': 0}])
        print('Demo passed: real ClickHouse server, synthetic fixtures, local mock.', flush=True)
        return 0
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, OSError) as error:
        print('Demo failed: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
