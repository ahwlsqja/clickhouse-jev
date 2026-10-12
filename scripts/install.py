#!/usr/bin/env python3
"""명시한 디렉터리에만 ClickHouse 실행 UDF 파일을 생성한다."""

import argparse
import math
import shlex
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('양의 정수가 필요하다')
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('유한한 양수가 필요하다')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--package-root', '--package-dir', type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--expected-model', required=True)
    for name, default in [('batch-size', 20), ('concurrency', 4),
                          ('max-block-rows', 10000), ('max-row-chars', 10000),
                          ('max-request-bytes', 1000000), ('pool-size', 1)]:
        parser.add_argument('--' + name, type=positive_int, default=default)
    parser.add_argument('--deadline-seconds', type=positive_float, default=30.0)
    parser.add_argument('--state-format', choices=['string', 'object'], default='string')
    parser.add_argument('--allow-http', action='store_true')
    credential = parser.add_mutually_exclusive_group()
    credential.add_argument('--api-key-fd', type=int)
    credential.add_argument('--api-key-env')
    args = parser.parse_args(argv)
    if args.api_key_fd is not None and args.api_key_fd < 0:
        parser.error('--api-key-fd는 음수가 될 수 없다')

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    package_root = args.package_root.resolve()
    flags = []
    names = ['endpoint', 'model', 'expected_model', 'batch_size', 'concurrency',
             'max_block_rows', 'max_row_chars', 'max_request_bytes',
             'deadline_seconds', 'state_format']
    for name in names:
        flags.extend(['--' + name.replace('_', '-'), str(getattr(args, name))])
    if args.allow_http:
        flags.append('--allow-http')
    if args.api_key_fd is not None:
        flags.extend(['--api-key-fd', str(args.api_key_fd)])
    if args.api_key_env is not None:
        flags.extend(['--api-key-env', args.api_key_env])
    command = shlex.join([args.python, '-m', 'clickhouse_jev', *flags])
    wrapper = output / 'jev-worker'
    wrapper.write_text('#!/bin/sh\nexport PYTHONPATH=' + shlex.quote(str(package_root))
                       + '\nexec ' + command + ' "$@"\n', encoding='utf-8')
    wrapper.chmod(0o755)

    functions = ET.Element('functions')
    function = ET.SubElement(functions, 'function')
    values = {'type': 'executable_pool', 'name': 'jev_eval', 'return_type': 'String',
              'format': 'JSONEachRow', 'command': 'jev-worker', 'send_chunk_header': '1',
              'pool_size': str(args.pool_size), 'deterministic': '0',
              'max_command_execution_time': str(math.ceil(args.deadline_seconds + 5)),
              'command_read_timeout': str(math.ceil((args.deadline_seconds + 5) * 1000))}
    for name, value in values.items():
        ET.SubElement(function, name).text = value
    for name in ['row_json', 'question', 'kind', 'options_json']:
        arg = ET.SubElement(function, 'argument')
        ET.SubElement(arg, 'name').text = name
        ET.SubElement(arg, 'type').text = 'String'
    ET.indent(functions)
    ET.ElementTree(functions).write(output / 'jev_function.xml', encoding='utf-8',
                                    xml_declaration=True)

    config = ET.Element('clickhouse')
    for name, value in {'user_scripts_path': str(output) + '/',
                        'user_defined_path': str(output / 'user_defined') + '/',
                        'user_defined_executable_functions_config':
                        str(output / 'jev_function.xml')}.items():
        ET.SubElement(config, name).text = value
    ET.indent(config)
    ET.ElementTree(config).write(output / 'config.xml', encoding='utf-8',
                                 xml_declaration=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'sql' / 'functions.sql',
                    output / 'functions.sql')
    print('설치 파일 생성 완료')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
