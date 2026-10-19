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
The fake Server, in this process, with no socket.

Every test below drives the real client against the real fake — the same one
`make fake-content-server-run` and the k3d deployment serve — so what passes here is
what a developer then sees end to end. A folder on disk is the tree: a test
writes, edits and deletes files, and the next request sees it.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from pathlib import Path

import httpx
import pytest

from fake_content_server import Credentials, FakeContentServer
from fake_content_server.server import NAME_SLASH_ON_DISK
from opentext_kb.api_manager import ApiManager, Subscription
from opentext_kb.content_server import Account, ContentServerSource
from opentext_kb.document_boundary import IngestionPending

ROOT_ID = "2000"
ACCOUNT = Account(
    name="team-account",
    password="team-password",  # pragma: allowlist secret
)
# Another team's account, accepted by the fake beside the first.
OTHER_ACCOUNTS = (
    Account(
        name="other-team-account",
        password="other-team-password",  # pragma: allowlist secret
    ),
)
SUBSCRIPTION = Subscription(
    api_key="test-key",  # pragma: allowlist secret
    api_secret="test-secret",  # pragma: allowlist secret
    username="apim-user",
)
BASE_URL = "https://server.example.com/corp/server-business/v2"


class Server:
    """Server for one test — the fake Content Server behind the API Manager, or
    reached directly — as files on disk, with faults on demand."""

    def __init__(self, root: Path, *, behind_api_manager: bool = True) -> None:
        self.root = root
        self.fake = FakeContentServer(
            root,
            root_id=ROOT_ID,
            credentials=Credentials(
                account=ACCOUNT.name,
                password=ACCOUNT.password,
                api_key=SUBSCRIPTION.api_key if behind_api_manager else "",
                api_secret=SUBSCRIPTION.api_secret,
                username=SUBSCRIPTION.username,
                other_accounts=tuple(
                    (other.name, other.password) for other in OTHER_ACCOUNTS
                ),
            ),
        )
        self.requests: list[httpx.Request] = []
        # Faults a test can turn on, each one something a real Server does.
        self.status: int | None = None
        self.fail_content: set[str] = set()
        self.busy_once: set[str] = set()

    # ── the tree ──────────────────────────────────────────────────────────────

    def put(
        self, path: str, content: bytes = b"content", *, mtime: int = 1_790_000_000
    ) -> None:
        """Hold a file at `path`, a Server path with `/` between folders."""
        target = self._on_disk(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        os.utime(target, (mtime, mtime))

    def folder(self, path: str) -> None:
        self._on_disk(path).mkdir(parents=True, exist_ok=True)

    def remove(self, path: str) -> None:
        self._on_disk(path).unlink()

    def node_id(self, path: str) -> str:
        return self.fake.node_id(self._on_disk(path).relative_to(self.root).as_posix())

    def _on_disk(self, path: str) -> Path:
        # A `/` inside a name is written `\` in a test and stored as the fake's marker.
        return self.root.joinpath(
            *(part.replace("\\", NAME_SLASH_ON_DISK) for part in path.split("/"))
        )

    # ── the server ────────────────────────────────────────────────────────────

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status is not None:
            return httpx.Response(self.status, text="refused")
        url = str(request.url)
        if url in self.busy_once:
            self.busy_once.discard(url)
            return httpx.Response(503, headers={"Retry-After": "0"})
        node = request.url.path.rstrip("/").split("/")[-2]
        if request.url.path.endswith("/content") and node in {
            self.node_id(p) for p in self.fail_content
        }:
            return httpx.Response(500, text="broken")
        answer = self.fake.respond(
            request.method,
            url,
            {name.lower(): value for name, value in request.headers.items()},
        )
        return httpx.Response(
            answer.status,
            content=answer.body,
            headers={"Content-Type": answer.content_type},
        )

    def listing_url(self, node_id: str, page: int = 1, page_size: int = 500) -> str:
        return f"{BASE_URL}/nodes/{node_id}/nodes?limit={page_size}&page={page}"


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path / "server")


def source_for(
    server: Server,
    *,
    folder_id: str = ROOT_ID,
    max_file_bytes: int = 1_000_000,
    page_size: int = 500,
    account: Account = ACCOUNT,
    subscription: Subscription | None = SUBSCRIPTION,
) -> ContentServerSource:
    """The real client, pointed at the fake through an in-memory transport."""
    server.root.mkdir(parents=True, exist_ok=True)
    return ContentServerSource(
        folder_id=folder_id,
        account=account,
        gateway=ApiManager(subscription) if subscription is not None else None,
        max_file_bytes=max_file_bytes,
        base_url=BASE_URL,
        page_size=page_size,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(server.handler),
            auth=httpx.BasicAuth(account.name, account.password),
            follow_redirects=False,
        ),
    )


class RecordingBoundary:
    """A library that holds what it accepted, and can refuse on demand.

    `contents` is the library itself and survives across runs, which is what
    makes a second run incremental now that nothing is kept on disk. The two
    logs are per run, and `new_run` is what clears them.
    """

    def __init__(
        self,
        *,
        holds: Mapping[str, str | None] | None = None,
        fail_publish: Collection[str] = (),
        fail_retract: Collection[str] = (),
        still_ingesting: Collection[str] = (),
        pending: Collection[str] = (),
        fail_listing: bool = False,
    ) -> None:
        self.contents: dict[str, str | None] = dict(holds or {})
        self.published: dict[str, tuple[bytes, str]] = {}
        self.retracted: list[str] = []
        self.fail_listing = fail_listing
        # Accepted and still being ingested: written, and correctly absent from
        # the listing until it finishes.
        self.still_ingesting = set(still_ingesting)
        # Accepted, listed as in progress, and still running when the wait ended.
        self.pending = set(pending)
        self.fail_publish = set(fail_publish)
        self.fail_retract = set(fail_retract)

    def new_run(self) -> None:
        self.published.clear()
        self.retracted.clear()

    async def publish(
        self, *, relative_path: str, content: bytes, version: str
    ) -> None:
        if relative_path in self.fail_publish:
            raise RuntimeError(f"refused {relative_path}")
        self.published[relative_path] = (content, version)
        if relative_path in self.pending:
            self.contents[relative_path] = version or None
            raise IngestionPending(f"{relative_path} still running")
        if relative_path not in self.still_ingesting:
            self.contents[relative_path] = version or None

    async def documents(self) -> dict[str, str | None]:
        if self.fail_listing:
            raise RuntimeError("the library could not be read")
        return dict(self.contents)

    async def retract(self, *, relative_path: str) -> None:
        if relative_path in self.fail_retract:
            raise RuntimeError(f"refused {relative_path}")
        self.retracted.append(relative_path)
        self.contents.pop(relative_path, None)

    async def aclose(self) -> None:
        return None


def settings_for(
    *,
    folder_id: str = ROOT_ID,
    include: str = "**/*.md",
    exclude: str = "",
    max_files: int | None = None,
    trust_any_certificate: bool = False,
):
    """The settings a team's form would produce for the test folder."""
    from opentext_kb.settings import read_settings

    return read_settings(
        {
            "folder_id": folder_id,
            "account": ACCOUNT.name,
            "password": ACCOUNT.password,
            "include": include,
            "exclude": exclude,
            "trust_any_certificate": trust_any_certificate,
            "max_files": max_files,
        }
    )


@pytest.fixture(autouse=True)
def _no_ambient_fred_configuration(monkeypatch: pytest.MonkeyPatch):
    """No test may pick up the configuration a developer happens to have.

    A pod resolves `./config/configuration.yaml` by default, so a suite run
    from this directory would otherwise reach the real Control Plane and
    Knowledge Flow of whoever ran it.
    """
    monkeypatch.setenv("CONFIG_FILE", "/nonexistent/configuration.yaml")
    monkeypatch.setenv("ENV_FILE", "/nonexistent/.env")
    for name in (
        "OPENTEXT_KB_API_KEY",
        "OPENTEXT_KB_API_SECRET",
        "OPENTEXT_KB_CA_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
