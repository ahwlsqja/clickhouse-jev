#!/usr/bin/env python3
"""Run end-to-end tests with ClickHouse and a loopback mock in the development container."""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path


def main():
    if not shutil.which('clickhouse-local'):
        print('Run this command in the ClickHouse development container', file=sys.stderr)
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
