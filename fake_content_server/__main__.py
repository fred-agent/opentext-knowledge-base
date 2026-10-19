# Copyright Thales 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Serve the fake Content Server over HTTP: `python -m fake_content_server`.

The standard library's server and nothing else, so the fake adds no dependency
to anything. Every setting has a default, so `make fake-content-server-run` and the k3d
deployment start it the same way; `FAKE_CONTENT_SERVER_*` variables override them.
"""

from __future__ import annotations

import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fake_content_server.server import Credentials, FakeContentServer

logger = logging.getLogger("fake_content_server")

# Not secrets: the fake's own credentials, the same for everybody who runs it,
# and accepted by nothing but the fake.
DEFAULTS = {
    "FAKE_CONTENT_SERVER_ROOT_DIR": str(Path(__file__).parent / "sample"),
    "FAKE_CONTENT_SERVER_ROOT_ID": "2000",
    "FAKE_CONTENT_SERVER_PORT": "8080",
    "FAKE_CONTENT_SERVER_ACCOUNT": "fake-account",
    "FAKE_CONTENT_SERVER_PASSWORD": "fake-password",  # nosec B105  # pragma: allowlist secret
    "FAKE_CONTENT_SERVER_API_KEY": "fake-api-key",  # pragma: allowlist secret
    "FAKE_CONTENT_SERVER_API_SECRET": "fake-api-secret",  # nosec B105  # pragma: allowlist secret
    "FAKE_CONTENT_SERVER_USERNAME": "fake-apim-user",
    # thales-api-manager: behind the API Manager gateway; none: Content Server directly.
    "FAKE_CONTENT_SERVER_GATEWAY": "thales-api-manager",
}


def setting(name: str) -> str:
    return os.environ.get(name) or DEFAULTS[name]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    root = Path(setting("FAKE_CONTENT_SERVER_ROOT_DIR")).resolve()
    behind_api_manager = setting("FAKE_CONTENT_SERVER_GATEWAY") == "thales-api-manager"
    fake = FakeContentServer(
        root,
        root_id=setting("FAKE_CONTENT_SERVER_ROOT_ID"),
        credentials=Credentials(
            account=setting("FAKE_CONTENT_SERVER_ACCOUNT"),
            password=setting("FAKE_CONTENT_SERVER_PASSWORD"),
            api_key=setting("FAKE_CONTENT_SERVER_API_KEY")
            if behind_api_manager
            else "",
            api_secret=setting("FAKE_CONTENT_SERVER_API_SECRET"),
            username=setting("FAKE_CONTENT_SERVER_USERNAME"),
        ),
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # The URL as the client signed it: scheme and host do not take part.
            answer = fake.respond(
                "GET",
                f"http://fake-content-server{self.path}",
                {name.lower(): value for name, value in self.headers.items()},
            )
            self.send_response(answer.status)
            self.send_header("Content-Type", answer.content_type)
            self.send_header("Content-Length", str(len(answer.body)))
            self.end_headers()
            self.wfile.write(answer.body)

        def log_message(self, format: str, *args: object) -> None:
            logger.info("%s %s", self.address_string(), format % args)

    port = int(setting("FAKE_CONTENT_SERVER_PORT"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # nosec B104 - a container's own port
    logger.info(
        "fake Content Server serving %s as folder %s on :%s (gateway: %s)",
        root,
        setting("FAKE_CONTENT_SERVER_ROOT_ID"),
        port,
        setting("FAKE_CONTENT_SERVER_GATEWAY"),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
