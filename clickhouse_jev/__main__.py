"""ClickHouse executable UDF의 명령행 진입점."""
import argparse
import os
import sys

from .transport import Config, HttpTransport, read_api_key, validate_api_key
from .worker import serve


class SafeParser(argparse.ArgumentParser):
    """사용자 값이 포함된 argparse 오류 문장을 출력하지 않는다."""

    def error(self, message):
        raise ValueError('worker_arguments_invalid')


def main(argv=None):
    try:
        parser = SafeParser(description='Jev JSONEachRow 블록 판정')
        for name in ('endpoint', 'model', 'expected_model'):
            parser.add_argument('--' + name.replace('_', '-'), required=True)
        for name in ('batch_size', 'concurrency', 'max_block_rows', 'max_row_chars', 'max_request_bytes'):
            parser.add_argument('--' + name.replace('_', '-'), type=int, default=getattr(Config, name))
        parser.add_argument('--deadline-seconds', type=float, default=Config.deadline_seconds)
        parser.add_argument('--state-format', choices=('string', 'object'), default=Config.state_format)
        parser.add_argument('--allow-http', action='store_true')
        credentials = parser.add_mutually_exclusive_group()
        credentials.add_argument('--api-key-fd', type=int)
        credentials.add_argument('--api-key-env')
        args = parser.parse_args(argv)
        values = vars(args).copy()
        api_key_fd = values.pop('api_key_fd')
        api_key_env = values.pop('api_key_env')
        config = Config(**values)
        if api_key_fd is not None:
            key = read_api_key(api_key_fd)
        elif api_key_env is not None:
            key = validate_api_key(os.environ.pop(api_key_env))
        else:
            key = None
        serve(sys.stdin, sys.stdout, config, HttpTransport(config, key))
        return 0
    except SystemExit as error:
        return int(error.code)
    except (Exception, KeyboardInterrupt):
        sys.stderr.write('jev_worker_failed\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
