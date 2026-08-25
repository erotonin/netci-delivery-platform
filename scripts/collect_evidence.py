from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    parser.add_argument('command', nargs='+')
    args = parser.parse_args()

    started = datetime.now(timezone.utc)
    start = time.perf_counter()
    completed = subprocess.run(args.command, capture_output=True, text=True, check=False)
    ended = datetime.now(timezone.utc)

    output = {
        'name': args.name,
        'command': args.command,
        'startedAt': started.isoformat(),
        'endedAt': ended.isoformat(),
        'durationSeconds': time.perf_counter() - start,
        'exitCode': completed.returncode,
        'success': completed.returncode == 0,
        'stdout': completed.stdout,
        'stderr': completed.stderr,
        'environment': 'windows-skeleton' if completed.returncode == 0 else 'unknown',
    }
    target = Path('evidence') / f'{args.name}.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2), encoding='utf-8')
    print(target)
    raise SystemExit(completed.returncode)


if __name__ == '__main__':
    main()
