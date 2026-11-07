"""Record a bounded read-only navigation sample from the existing local API.

uv run python scripts/record_go2_navigation.py --seconds 10
The robot must already be connected. No movement or connection is started.
Artifacts stay on the API host under logs/go2-search/recording_<id>/.
Replay voxel files with scripts/replay_go2_search.py. No VLM calls are made.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import urllib.request

from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60:
        parser.error('seconds must be in (0, 60]')
    load_dotenv(Path(__file__).resolve().parents[1] / 'cloud' / '.env')
    token = os.getenv('SSM_INTERNAL_SERVICE_TOKEN')
    if not token:
        parser.error('SSM_INTERNAL_SERVICE_TOKEN is not configured')
    request = urllib.request.Request('http://127.0.0.1:8082/api/go2/diagnostics/navigation-recording',
        data=json.dumps({'duration_s': args.seconds}).encode(), method='POST', headers={
            'Content-Type': 'application/json', 'X-SSM-Service-Token': token,
            'X-SSM-Session-Id': 'navigation-recording', 'X-SSM-Actor': 'admin',
            'X-SSM-Persist': 'true', 'X-SSM-Expires-At': str(int(time.time() + args.seconds + 30)),
        })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=args.seconds + 20) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
