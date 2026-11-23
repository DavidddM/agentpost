"""Exit 0 if the local server answers /healthz. Used by the Docker HEALTHCHECK.

Reads AGENTPOST_PORT like the server does, and ignores proxy settings because
the check never leaves the container.
"""

import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("AGENTPOST_PORT", "8765")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
