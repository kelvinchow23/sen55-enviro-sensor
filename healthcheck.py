#!/usr/bin/env python3
"""Container HEALTHCHECK probe. Exits 0 only when the sampler reports state
"ok"; any other state, a bad response, or a connection failure exits 1, which
Docker marks as unhealthy. Kept as its own script (not an inline `python -c`)
so the logic is readable and the base image needs no curl/wget."""

import json
import sys
import urllib.request

URL = "http://127.0.0.1:8000/health"

try:
    with urllib.request.urlopen(URL, timeout=4) as resp:
        payload = json.load(resp)
    sys.exit(0 if payload.get("state") == "ok" else 1)
except Exception as e:  # connection refused, timeout, bad JSON, etc.
    print(f"healthcheck failed: {e}", file=sys.stderr)
    sys.exit(1)
