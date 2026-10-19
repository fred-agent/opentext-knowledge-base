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
"""The Content Server client, against the fake: the walk, a read, every refusal."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from opentext_kb import content_server as server_module
from opentext_kb.api_manager import Subscription, canonical_resource, signed_headers
from opentext_kb.content_server import SLASH_IN_NAME, Account, NotAFolder
from opentext_kb.source import FileTooLarge, RemoteFile, SourceUnavailable
from tests.conftest import SUBSCRIPTION, Server, source_for

# Computed by the gateway's reference client, the one it has been accepting in
# production, for this secret, URL and timestamp. If this moves, the client no
# longer signs the way the API Manager accepts.
GATEWAY_URL = (
    "https://gateway.example.com/otcs/api/v2/nodes/123456/nodes?limit=500&page=2"
)
GATEWAY_SIGNATURE = "BDq6OFzD9+wudcEqVMdRBg4Arexk/hK4Jf5ucrbOTjTieKlP+k/YOBPIHSUO/uLmI+lFuEDjkAGzopsfPQz9/g=="  # pragma: allowlist secret


def test_the_signature_is_the_reference_clients():
    subscription = Subscription(
        api_key="key-123",  # pragma: allowlist secret
        api_secret="s3cr3t",  # pragma: allowlist secret
        username="u",
    )
    headers = signed_headers(
        "GET",
        GATEWAY_URL,
        subscription=subscription,
        now=datetime(2026, 10, 19, 14, 5, 9, tzinfo=UTC),
    )

    assert headers["apisecret"] == GATEWAY_SIGNATURE
    assert headers["X-Apim-Timestamp"] == "2026-10-19T14:05:09.000"
    assert headers["apikey"] == "key-123"  # pragma: allowlist secret
    assert headers["username"] == "u"


def test_the_signed_resource_encodes_the_query_as_the_gateway_does():
    assert canonical_resource("https://x/v2/nodes/9/nodes?q=it's (a)!~&limit=5") == (
        "/v2/nodes/9/nodes?q=it%27s%20%28a%29%21%7E&limit=5"
    )


def test_an_unknown_hash_algorithm_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="SHA-1"):
        signed_headers(
            "GET",
            GATEWAY_URL,
            subscription=Subscription(
                api_key="k", api_secret="s", username="u", hash_algorithm="SHA-1"
            ),
            now=datetime.now(UTC),
        )


async def test_the_walk_finds_every_file_under_the_folder(server: Server):
    server.put("a.md", b"a", mtime=1_790_000_000)
    server.put("Projets/2026/note.md", b"note")
    server.folder("Vide")

    inventory = await source_for(server).inventory()

    files = {file.path: file for file in inventory.files}
    assert set(files) == {"a.md", "Projets/2026/note.md"}
    assert inventory.exhaustive
    assert files["a.md"].version == "mtime:2026-09-21T14:13:20+0000:1"
    assert files["a.md"].size_bytes == 1
    assert files["Projets/2026/note.md"].ref == server.node_id("Projets/2026/note.md")


async def test_every_page_of_a_folder_is_read(server: Server):
    for index in range(5):
        server.put(f"doc-{index}.md")

    inventory = await source_for(server, page_size=2).inventory()

    assert len(inventory.files) == 5
    assert [request.url.params["page"] for request in server.requests] == [
        "1",
        "2",
        "3",
    ]


async def test_a_slash_in_a_name_stays_in_the_name(server: Server):
    server.put("Comptes rendus\\revue.md")

    inventory = await source_for(server).inventory()

    assert [file.path for file in inventory.files] == [
        f"Comptes rendus{SLASH_IN_NAME}revue.md"
    ]


async def test_every_request_carries_both_identities(server: Server):
    server.put("a.md")

    await source_for(server).inventory()

    request = server.requests[0]
    assert request.headers["apikey"] == SUBSCRIPTION.api_key
    assert request.headers["authorization"].startswith("Basic ")
    assert request.headers["apisecret"]


async def test_a_document_is_read_by_its_node(server: Server):
    server.put("dossier/a.md", b"le contenu")
    source = source_for(server)
    (file,) = (await source.inventory()).files

    assert await source.read(file) == b"le contenu"


async def test_more_bytes_than_agreed_are_refused_while_reading(server: Server):
    """The listed size is Server's claim; the bound holds on what arrives."""
    server.put("big.md", b"x" * 100)
    file = RemoteFile(
        path="big.md", version="v", size_bytes=1, ref=server.node_id("big.md")
    )

    with pytest.raises(FileTooLarge):
        await source_for(server, max_file_bytes=10).read(file)


async def test_a_busy_server_is_asked_again(server: Server):
    server.put("a.md")
    server.busy_once.add(server.listing_url(server.fake.node_id("")))

    inventory = await source_for(server).inventory()

    assert [file.path for file in inventory.files] == ["a.md"]


async def test_a_wrong_account_is_named_as_such(server: Server):
    server.put("a.md")
    source = source_for(
        server, account=Account(name="team-account", password="wrong")
    )  # pragma: allowlist secret

    with pytest.raises(SourceUnavailable, match="Content Server account"):
        await source.inventory()


async def test_a_wrong_signature_is_refused(server: Server):
    server.put("a.md")
    wrong = Subscription(
        api_key=SUBSCRIPTION.api_key,
        api_secret="not-it",  # pragma: allowlist secret
        username=SUBSCRIPTION.username,
    )

    with pytest.raises(SourceUnavailable, match="refused"):
        await source_for(server, subscription=wrong).inventory()


async def test_an_unknown_folder_is_not_a_folder(server: Server):
    with pytest.raises(NotAFolder):
        await source_for(server, folder_id="1234567").inventory()


async def test_a_document_named_as_the_folder_is_not_a_folder(server: Server):
    server.put("a.md")

    with pytest.raises(NotAFolder):
        await source_for(server, folder_id=server.node_id("a.md")).inventory()


async def test_a_walk_past_its_depth_bound_says_it_saw_only_part(
    server: Server, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(server_module, "MAX_DEPTH", 1)
    server.put("top.md")
    server.put("one/two/deep.md")

    inventory = await source_for(server).inventory()

    assert not inventory.exhaustive
    assert [file.path for file in inventory.files] == ["top.md"]


async def test_a_server_that_keeps_failing_stops_the_walk(
    server: Server, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(server_module, "BACKOFF_SECONDS", 0.0)
    server.put("a.md")
    server.status = 500

    with pytest.raises(SourceUnavailable):
        await source_for(server).inventory()

    assert len(server.requests) == server_module.MAX_ATTEMPTS


async def test_without_a_gateway_no_gateway_header_is_sent(tmp_path):
    """Content Server reached directly: the account alone, nothing of Thales's."""
    direct = Server(tmp_path / "direct", behind_api_manager=False)
    direct.put("a.md", b"a")

    inventory = await source_for(direct, subscription=None).inventory()

    assert [file.path for file in inventory.files] == ["a.md"]
    sent = {name.lower() for name in direct.requests[0].headers}
    assert "authorization" in sent
    assert not sent & {"apikey", "apisecret", "username", "x-apim-timestamp"}
