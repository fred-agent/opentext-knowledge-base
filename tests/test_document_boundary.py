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
The boundary a dispatched run really uses, over a publisher that answers as
Fred does: a write is accepted with a task, and the wait says how it ended.

The property under test is that `publish` returns only for a document that
landed — so a run's report never counts as written what Fred then dropped.
"""

from __future__ import annotations

import logging
from typing import cast

import pytest
from fred_sdk.knowledge_base import (
    DocumentHandle,
    DocumentOutcome,
    DocumentPublisher,
    DocumentWaitTimeout,
)

from opentext_kb.document_boundary import (
    IngestionPending,
    _KnowledgeFlowBoundary,
)
from opentext_kb.settings import IngestionProfile

TASK_ID = "task-1"


class FakePublisher:
    """Accepts every write, then answers the wait with what a test decided."""

    def __init__(self, ending: DocumentOutcome | Exception) -> None:
        self.ending = ending
        self.published: list[tuple[str, bytes, str | None]] = []
        self.waited: list[str] = []
        self.timeouts: list[float] = []
        self.profiles: list[str] = []

    async def publish(
        self,
        *,
        relative_path: str,
        content: bytes,
        version: str | None = None,
        profile: IngestionProfile = "medium",
    ) -> DocumentHandle:
        self.published.append((relative_path, content, version))
        self.profiles.append(profile)
        return DocumentHandle(
            task_id=TASK_ID,
            document_uid="doc-1",
            source_key=relative_path,
            document_version=version,
            created=True,
        )

    async def wait(
        self, task_id: str, *, timeout: float = 600.0, poll_interval: float = 2.0
    ) -> DocumentOutcome:
        self.waited.append(task_id)
        self.timeouts.append(timeout)
        if isinstance(self.ending, Exception):
            raise self.ending
        return self.ending

    async def documents(self) -> dict[str, str | None]:
        return {}

    async def retract(self, *, relative_path: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


def boundary_over(
    ending: DocumentOutcome | Exception,
) -> tuple[_KnowledgeFlowBoundary, FakePublisher]:
    publisher = FakePublisher(ending)
    # The boundary is typed against the SDK's publisher; this one only has to
    # answer the same calls.
    return _KnowledgeFlowBoundary(cast(DocumentPublisher, publisher)), publisher


def landed() -> DocumentOutcome:
    return DocumentOutcome(task_id=TASK_ID, state="succeeded")


async def test_a_write_returns_once_the_ingestion_it_started_has_succeeded(
    caplog: pytest.LogCaptureFixture,
):
    boundary, publisher = boundary_over(landed())

    with caplog.at_level(logging.INFO):
        await boundary.publish(relative_path="a.md", content=b"a", version="etag:v1")

    assert publisher.published == [("a.md", b"a", "etag:v1")]
    assert publisher.waited == [TASK_ID]
    assert publisher.profiles == ["medium"]
    # The one trace that ties a document to the task Fred ran for it.
    assert TASK_ID in caplog.text


@pytest.mark.parametrize(
    ("state", "error", "said"),
    [
        ("failed", "no processor for .xyz", "ingestion failed: no processor for .xyz"),
        ("cancelled", None, "ingestion cancelled: no reason given"),
    ],
)
async def test_an_ingestion_that_did_not_land_is_this_document_s_failure(
    state: str, error: str | None, said: str
):
    """Fred accepted the write, then did not keep it: the run must not count it."""
    boundary, publisher = boundary_over(
        DocumentOutcome(task_id=TASK_ID, state=state, error=error)
    )

    with pytest.raises(RuntimeError, match=said):
        await boundary.publish(relative_path="a.xyz", content=b"a", version="etag:v1")

    assert publisher.waited == [TASK_ID]


async def test_a_wait_that_ends_first_is_not_dressed_up_as_a_write_failure():
    """The task is still running, not lost: said as its own error, naming it."""
    boundary, _ = boundary_over(DocumentWaitTimeout(TASK_ID))

    with pytest.raises(IngestionPending, match=TASK_ID):
        await boundary.publish(relative_path="a.md", content=b"a", version="etag:v1")


async def test_the_wait_lasts_what_the_deployment_configured():
    publisher = FakePublisher(landed())
    boundary = _KnowledgeFlowBoundary(
        cast(DocumentPublisher, publisher), wait_seconds=1800
    )

    await boundary.publish(relative_path="a.md", content=b"a", version="v1")

    assert publisher.timeouts == [1800]


async def test_an_empty_version_is_offered_as_none_at_all():
    """A share with no entity tags has no version, which is not an empty one."""
    boundary, publisher = boundary_over(landed())

    await boundary.publish(relative_path="a.md", content=b"a", version="")

    assert publisher.published == [("a.md", b"a", None)]


@pytest.mark.parametrize("profile", ["fast", "medium", "rich"])
async def test_the_selected_profile_reaches_the_publisher(profile: IngestionProfile):
    publisher = FakePublisher(landed())
    boundary = _KnowledgeFlowBoundary(
        cast(DocumentPublisher, publisher), profile=profile
    )

    await boundary.publish(relative_path="a.md", content=b"a", version="v1")

    assert publisher.profiles == [profile]
    assert publisher.waited == [TASK_ID]
