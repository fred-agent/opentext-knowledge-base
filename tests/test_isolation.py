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
One pod, several teams: each run reads with its own account and writes into its
own library, even when they run at the same time.

Driven through the handler a dispatched run calls, so what is shown is what
Fred gets: nothing a run builds outlives it, and nothing one team configured
reaches another team's run.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from fred_sdk.knowledge_base import KnowledgeBaseRunContext, KnowledgeBaseRunOutcome

from opentext_kb import knowledge_base
from opentext_kb.content_server import ContentServerSource
from opentext_kb.deployment import (
    API_KEY_ENV,
    API_SECRET_ENV,
    ApiManagerSettings,
    OpenTextDeployment,
)
from tests.conftest import (
    ACCOUNT,
    OTHER_ACCOUNTS,
    SUBSCRIPTION,
    RecordingBoundary,
    Server,
)

TEAMS = {
    "team-a": ("Equipe A", ACCOUNT),
    "team-b": ("Equipe B", OTHER_ACCOUNTS[0]),
}


@pytest.fixture
def pod(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> dict[str, RecordingBoundary]:
    """The handler, with the fake for Server and one recorded library per run."""
    monkeypatch.setenv(API_KEY_ENV, SUBSCRIPTION.api_key)
    monkeypatch.setenv(API_SECRET_ENV, SUBSCRIPTION.api_secret)
    monkeypatch.setattr(
        knowledge_base,
        "OpenTextDeployment",
        lambda: OpenTextDeployment(
            base_url="https://server.example.com/v2",
            gateway="thales-api-manager",
            api_manager=ApiManagerSettings(username=SUBSCRIPTION.username),
        ),
    )
    libraries: dict[str, RecordingBoundary] = {}

    def boundary(_pod, *, library_id: str, profile: str) -> RecordingBoundary:
        return libraries.setdefault(library_id, RecordingBoundary())

    def source(**options) -> ContentServerSource:
        account = options["account"]
        return ContentServerSource(
            **options,
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(server.handler),
                auth=httpx.BasicAuth(account.name, account.password),
            ),
        )

    monkeypatch.setattr(knowledge_base, "open_boundary", boundary)
    monkeypatch.setattr(knowledge_base, "ContentServerSource", source)
    return libraries


def _context(server: Server, team: str) -> KnowledgeBaseRunContext:
    folder, account = TEAMS[team]
    return KnowledgeBaseRunContext(
        definition_id="fred.opentext.folder",
        instance_id=f"instance-{team}",
        team_id=team,
        run_id=f"run-{team}",
        library_id=f"library-{team}",
        configuration={
            "folder_id": server.node_id(folder),
            "account": account.name,
            "password": account.password,
            "include": "**/*.md",
        },
    )


async def test_two_teams_run_at_once_each_into_its_own_library(
    server: Server, pod: dict[str, RecordingBoundary]
):
    server.put("Equipe A/plan.md", b"a")
    server.put("Equipe A/notes/a.md", b"a")
    server.put("Equipe B/budget.md", b"b")

    results = await asyncio.gather(
        knowledge_base.synchronize(_context(server, "team-a")),
        knowledge_base.synchronize(_context(server, "team-b")),
    )

    assert [result.outcome for result in results] == [
        KnowledgeBaseRunOutcome.succeeded
    ] * 2
    assert sorted(pod["library-team-a"].contents) == ["notes/a.md", "plan.md"]
    assert sorted(pod["library-team-b"].contents) == ["budget.md"]


async def test_each_team_s_folder_is_read_only_with_that_team_s_account(
    server: Server, pod: dict[str, RecordingBoundary]
):
    server.put("Equipe A/plan.md", b"a")
    server.put("Equipe B/budget.md", b"b")
    owner = {
        server.node_id(path): team
        for team, (folder, _) in TEAMS.items()
        for path in (folder, *(f"{folder}/{name}" for name in ("plan.md", "budget.md")))
        if (server.root / path).exists()
    }

    await asyncio.gather(
        knowledge_base.synchronize(_context(server, "team-a")),
        knowledge_base.synchronize(_context(server, "team-b")),
    )

    for request in server.requests:
        node = request.url.path.rstrip("/").split("/")[-2]
        name = (
            base64.b64decode(request.headers["authorization"].split()[1])
            .decode()
            .split(":")[0]
        )
        assert name == TEAMS[owner[node]][1].name, request.url


async def test_one_team_s_wrong_password_fails_only_that_team(
    server: Server, pod: dict[str, RecordingBoundary]
):
    server.put("Equipe A/plan.md", b"a")
    server.put("Equipe B/budget.md", b"b")
    wrong = _context(server, "team-b")
    wrong.configuration["password"] = "not-it"  # pragma: allowlist secret

    a, b = await asyncio.gather(
        knowledge_base.synchronize(_context(server, "team-a")),
        knowledge_base.synchronize(wrong),
    )

    assert a.outcome is KnowledgeBaseRunOutcome.succeeded
    assert b.outcome is KnowledgeBaseRunOutcome.failed
    assert sorted(pod["library-team-a"].contents) == ["plan.md"]
    assert pod["library-team-b"].contents == {}
