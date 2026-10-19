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
A whole run, against the fake Server, through the real client.

These are the tests that say what a team's library looks like afterwards — and
above all what survives a run that went wrong, now that the library itself is
the only record of what it holds.
"""

from __future__ import annotations

import asyncio

import pytest

from opentext_kb import content_server as server_module
from opentext_kb.synchronize import synchronize
from tests.conftest import RecordingBoundary, Server, settings_for, source_for


async def run(
    server: Server,
    boundary: RecordingBoundary | None = None,
    *,
    folder_id: str | None = None,
    max_file_bytes: int = 1_000_000,
    **options,
):
    """One synchronization, exactly as a dispatched run performs it."""
    boundary = boundary or RecordingBoundary()
    boundary.new_run()
    server.requests.clear()
    settings = settings_for(
        **({"folder_id": folder_id} if folder_id else {}), **options
    )
    source = source_for(
        server, folder_id=settings.folder_id, max_file_bytes=max_file_bytes
    )
    try:
        report = await synchronize(
            settings=settings,
            source=source,
            boundary=boundary,
            max_file_bytes=max_file_bytes,
        )
    finally:
        await source.aclose()
    return report, boundary


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_module, "BACKOFF_SECONDS", 0.0)


async def test_a_first_run_publishes_everything_into_an_empty_library(server: Server):
    server.put("a.md", b"a")
    server.put("docs/b.md", b"b")

    report, boundary = await run(server)

    assert report.succeeded
    assert report.exhaustive
    assert report.created == 2
    assert sorted(boundary.published) == ["a.md", "docs/b.md"]
    assert sorted(boundary.contents) == ["a.md", "docs/b.md"]


async def test_a_second_run_over_an_unchanged_folder_publishes_nothing(server: Server):
    server.put("a.md", b"a")
    _, boundary = await run(server)

    report, boundary = await run(server, boundary)

    assert boundary.published == {}
    assert report.unchanged == 1
    assert report.created == 0


async def test_an_edited_file_is_published_again_as_an_update(server: Server):
    server.put("a.md", b"a", mtime=1_790_000_000)
    _, boundary = await run(server)

    server.put("a.md", b"a changed", mtime=1_790_000_600)
    report, boundary = await run(server, boundary)

    assert report.updated == 1
    content, version = boundary.published["a.md"]
    assert content == b"a changed"
    assert version == "mtime:2026-09-21T14:23:20+0000:9"


async def test_a_file_server_dropped_is_retracted(server: Server):
    server.put("a.md", b"a")
    server.put("b.md", b"b")
    _, boundary = await run(server)

    server.remove("b.md")
    report, boundary = await run(server, boundary)

    assert boundary.retracted == ["b.md"]
    assert report.removed == 1
    assert list(boundary.contents) == ["a.md"]


async def test_a_document_the_library_does_not_list_is_published_again(server: Server):
    """The property that makes an accepted-but-unfinished write safe."""
    server.put("a.md", b"a")
    server.put("b.md", b"b")
    _, boundary = await run(server)

    # What a worker that failed after the 202 leaves behind.
    del boundary.contents["b.md"]
    report, boundary = await run(server, boundary)

    assert sorted(boundary.published) == ["b.md"]
    assert boundary.retracted == []
    assert report.created == 1
    assert report.unchanged == 1


async def test_a_document_still_being_ingested_is_offered_again(server: Server):
    """The price of keeping no record: unfinished and failed look the same."""
    server.put("a.md", b"a")
    _, boundary = await run(server, RecordingBoundary(still_ingesting={"a.md"}))

    report, boundary = await run(server, boundary)

    assert sorted(boundary.published) == ["a.md"]
    assert boundary.retracted == []
    assert report.unchanged == 0


async def test_a_document_the_library_holds_and_server_does_not_is_retracted(
    server: Server,
):
    """Whoever put it there: the listing is the record, not this pod's own past."""
    server.put("a.md", b"a")

    report, boundary = await run(
        server, RecordingBoundary(holds={"written-elsewhere.md": "v0"})
    )

    assert boundary.retracted == ["written-elsewhere.md"]
    assert report.removed == 1


async def test_a_server_that_cannot_be_reached_changes_nothing(server: Server):
    server.put("a.md", b"a")
    _, boundary = await run(server)
    before = dict(boundary.contents)

    server.status = 500
    report, boundary = await run(server, boundary)

    assert not report.succeeded
    assert [issue.code for issue in report.errors] == ["source_unavailable"]
    assert boundary.retracted == []
    assert boundary.contents == before


async def test_a_folder_id_naming_no_folder_is_the_teams_to_fix(server: Server):
    server.put("a.md", b"a")

    report, boundary = await run(server, folder_id="1234567")

    assert [issue.code for issue in report.errors] == ["not_a_folder"]
    assert boundary.published == {}


async def test_an_empty_folder_facing_a_full_library_removes_nothing(server: Server):
    """A renamed folder, a revoked right or a wrong id all list as empty.

    Reading that as "everything was deleted" would empty a team's library on a
    configuration mistake.
    """
    server.put("a.md", b"a")
    _, boundary = await run(server)

    server.remove("a.md")
    report, boundary = await run(server, boundary)

    assert boundary.retracted == []
    assert "a.md" in boundary.contents
    assert not report.exhaustive
    assert [issue.code for issue in report.warnings] == ["folder_empty"]


async def test_a_library_that_cannot_be_read_stops_the_run_before_anything(
    server: Server,
):
    """Reading a failed listing as an empty library retracts a team's documents."""
    server.put("a.md", b"a")

    report, boundary = await run(server, RecordingBoundary(fail_listing=True))

    assert not report.succeeded
    assert not report.exhaustive
    assert boundary.published == {}
    assert boundary.retracted == []
    assert [issue.code for issue in report.errors] == ["library_unreadable"]
    assert server.requests == []


async def test_a_failed_write_leaves_the_document_the_library_still_holds(
    server: Server,
):
    server.put("a.md", b"a", mtime=1_790_000_000)
    _, boundary = await run(server)
    held = dict(boundary.contents)

    server.put("a.md", b"a changed", mtime=1_790_000_600)
    boundary.fail_publish = {"a.md"}
    report, boundary = await run(server, boundary)
    assert not report.succeeded
    assert boundary.contents == held

    boundary.fail_publish = set()
    server.put("b.md", b"b")
    server.remove("a.md")
    _, boundary = await run(server, boundary)
    assert boundary.retracted == ["a.md"]


async def test_a_failed_read_keeps_the_document_and_reports_it(server: Server):
    server.put("a.md", b"a")
    server.put("b.md", b"b")
    _, boundary = await run(server)

    server.put("b.md", b"b changed", mtime=1_790_000_600)
    server.fail_content.add("b.md")
    report, boundary = await run(server, boundary)

    assert [(issue.code, issue.subject) for issue in report.errors] == [
        ("read_failed", "b.md")
    ]
    assert "b.md" in boundary.contents
    assert boundary.retracted == []


async def test_a_failed_retract_leaves_the_document_so_a_later_run_retries(
    server: Server,
):
    server.put("a.md", b"a")
    server.put("b.md", b"b")
    _, boundary = await run(server)

    server.remove("b.md")
    boundary.fail_retract = {"b.md"}
    report, boundary = await run(server, boundary)
    assert not report.succeeded
    assert "b.md" in boundary.contents

    boundary.fail_retract = set()
    _, boundary = await run(server, boundary)
    assert boundary.retracted == ["b.md"]


async def test_a_document_still_ingesting_is_neither_a_failure_nor_written_again(
    server: Server,
):
    server.put("a.md", b"a")
    server.put("b.md", b"b")

    report, boundary = await run(server, RecordingBoundary(pending={"b.md"}))

    assert report.succeeded
    assert report.pending == 1
    assert report.created == 1
    assert [issue.code for issue in report.warnings] == ["ingestion_pending"]

    boundary.pending = set()
    report, boundary = await run(server, boundary)
    assert boundary.published == {}
    assert report.unchanged == 2


class _CountingBoundary(RecordingBoundary):
    """Remembers how many writes were ever in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.most_in_flight = 0

    async def publish(self, *, relative_path: str, content: bytes, version: str):
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        await super().publish(
            relative_path=relative_path, content=content, version=version
        )


async def test_a_run_writes_as_many_documents_at_once_as_it_is_allowed(server: Server):
    for name in "abcdefgh":
        server.put(f"{name}.md", name.encode())
    boundary = _CountingBoundary()
    source = source_for(server)
    try:
        report = await synchronize(
            settings=settings_for(), source=source, boundary=boundary, concurrency=3
        )
    finally:
        await source.aclose()

    assert report.created == 8
    assert boundary.most_in_flight == 3


async def test_a_bounded_run_leaves_the_rest_alone_rather_than_retracting_it(
    server: Server,
):
    for name in "abc":
        server.put(f"{name}.md", name.encode())
    _, boundary = await run(server)

    report, boundary = await run(server, boundary, max_files=2)

    assert not report.exhaustive
    assert boundary.retracted == []
    assert sorted(boundary.contents) == ["a.md", "b.md", "c.md"]
    assert any(issue.code == "max_files_reached" for issue in report.warnings)


async def test_a_walk_cut_short_never_retracts(
    server: Server, monkeypatch: pytest.MonkeyPatch
):
    server.put("a.md", b"a")
    server.put("deep/b.md", b"b")
    _, boundary = await run(server)

    monkeypatch.setattr(server_module, "MAX_DEPTH", 0)
    report, boundary = await run(server, boundary)

    assert not report.exhaustive
    assert boundary.retracted == []
    assert "deep/b.md" in boundary.contents


async def test_a_file_too_large_is_skipped_and_keeps_its_document(server: Server):
    server.put("a.md", b"a")
    server.put("big.md", b"big")
    _, boundary = await run(server)

    server.put("big.md", b"x" * 100, mtime=1_790_000_600)
    report, boundary = await run(server, boundary, max_file_bytes=50)

    assert boundary.retracted == []
    assert report.skipped == 1
    assert "big.md" in boundary.contents


async def test_a_folder_full_of_other_files_still_reconciles_completely(server: Server):
    """The bound counts documents, so other files must not make every run partial."""
    for index in range(30):
        server.put(f"pictures/{index}.png", b"x")
    server.put("a.md", b"a")
    server.put("b.md", b"b")
    _, boundary = await run(server, max_files=10)

    server.remove("b.md")
    report, boundary = await run(server, boundary, max_files=10)

    assert report.exhaustive
    assert boundary.retracted == ["b.md"]


async def test_the_selection_ignores_case(server: Server):
    """Server is full of `.PDF` and `.MD`; a team writing `**/*.md` means them too."""
    server.put("NOTE.MD", b"n")
    server.put("image.PNG", b"i")

    _, boundary = await run(server)

    assert sorted(boundary.published) == ["NOTE.MD"]


async def test_accepting_any_certificate_is_reported_on_every_run(server: Server):
    server.put("a.md", b"a")

    report, _ = await run(server, trust_any_certificate=True)

    assert any(issue.code == "certificate_not_verified" for issue in report.warnings)


async def test_a_binary_file_reaches_the_library_byte_for_byte(server: Server):
    """Every byte here breaks something if any layer decides the content is text."""
    png = b"\x89PNG\r\n\x1a\n\x00\xff\xfe\r\n" + bytes(range(256))
    server.put("pictures/diagram.png", png)
    server.put("notes.md", b"text")

    _, boundary = await run(server, include="**/*.md,**/*.png")

    assert boundary.published["pictures/diagram.png"][0] == png
    assert sorted(boundary.published) == ["notes.md", "pictures/diagram.png"]
