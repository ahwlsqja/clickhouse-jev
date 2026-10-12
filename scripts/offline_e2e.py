#!/usr/bin/env python3
"""개발 컨테이너 내부에서 실제 CH와 loopback mock의 E2E를 실행한다."""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path


def main():
    if not shutil.which('clickhouse-local'):
        print('ClickHouse 개발 컨테이너에서 실행해야 한다', file=sys.stderr)
        return 1
    version = subprocess.run(['clickhouse-local', '--version-clean'], capture_output=True,
                             text=True, check=True).stdout.strip()
    print(f'ClickHouse {version}; Python {sys.version_info.major}.{sys.version_info.minor}', flush=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
    from test_integration import ClickHouseE2ETests
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ClickHouseE2ETests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
