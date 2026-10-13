"""Serve synthetic Jev responses on the internal demo network."""

import argparse
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.mock_api import MockAPI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('success', 'error'), default='success')
    args = parser.parse_args()
    stopped = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stopped.set())
    with MockAPI(mode=args.mode, address=('0.0.0.0', 8765)):
        print('Synthetic mock ready on the internal demo network.', flush=True)
        stopped.wait()


if __name__ == '__main__':
    main()
