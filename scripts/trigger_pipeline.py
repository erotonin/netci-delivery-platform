from __future__ import annotations

import argparse
import json
import os
import urllib.request
import uuid


def main() -> None:
    parser = argparse.ArgumentParser(description='Trigger netCI pipeline from Git push/webhook adapter')
    parser.add_argument('--api', default=os.getenv('NETCI_API_URL', 'http://localhost:8000'))
    parser.add_argument('--application-id', required=True)
    parser.add_argument('--commit-sha', required=True)
    parser.add_argument('--environment', choices=['dev', 'staging', 'prod'], default='dev')
    args = parser.parse_args()

    payload = json.dumps({'commitSha': args.commit_sha, 'environment': args.environment}).encode()
    request = urllib.request.Request(
        f'{args.api}/applications/{args.application_id}/pipeline-runs',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Idempotency-Key': str(uuid.uuid4()),
            'X-Correlation-Id': str(uuid.uuid4()),
        },
        method='POST',
    )
    with urllib.request.urlopen(request) as response:
        print(response.read().decode())


if __name__ == '__main__':
    main()
