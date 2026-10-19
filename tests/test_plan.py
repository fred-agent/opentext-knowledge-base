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
What a run decides before it touches anything.

The rule under test throughout is the one that decides whether a team keeps
its documents: an absence means a deletion only for a run that saw the whole
share.
"""

from __future__ import annotations

from opentext_kb.plan import MAX_SOURCE_KEY_LENGTH, plan_run
from opentext_kb.settings import Selection
from opentext_kb.source import Inventory, RemoteFile


def file(path: str, version: str = "v1", size: int = 10) -> RemoteFile:
    return RemoteFile(path=path, version=version, size_bytes=size)


def plan(
    *files: RemoteFile,
    previous: dict[str, str | None] | None = None,
    exhaustive: bool = True,
    include: str = "**/*.md",
    exclude: str = "",
    max_files: int | None = None,
    max_file_bytes: int = 1_000,
):
    return plan_run(
        Inventory(files=files, exhaustive=exhaustive),
        previous=previous or {},
        selection=Selection(include=(include,), exclude=(exclude,) if exclude else ()),
        max_files=max_files,
        max_file_bytes=max_file_bytes,
    )


def test_a_file_the_library_has_never_seen_is_written():
    result = plan(file("a.md"))
    assert [write.source_key for write in result.writes] == ["a.md"]


def test_a_file_whose_version_has_not_moved_is_left_alone():
    result = plan(file("a.md", "v1"), previous={"a.md": "v1"})
    assert result.writes == ()
    assert result.unchanged == ("a.md",)


def test_a_file_whose_version_moved_is_written_again():
    result = plan(file("a.md", "v2"), previous={"a.md": "v1"})
    assert [write.source_key for write in result.writes] == ["a.md"]


def test_a_file_the_share_dropped_is_removed_by_an_exhaustive_run():
    result = plan(file("a.md"), previous={"a.md": "v1", "gone.md": "v1"})
    assert [removal.source_key for removal in result.removals] == ["gone.md"]


def test_a_run_that_did_not_see_everything_removes_nothing():
    """The whole safety property: absence proves nothing about a share unseen."""
    result = plan(
        file("a.md"), previous={"a.md": "v1", "gone.md": "v1"}, exhaustive=False
    )
    assert result.removals == ()


def test_a_bounded_run_removes_nothing_and_names_what_it_left():
    result = plan(
        file("a.md"),
        file("b.md"),
        file("c.md"),
        previous={"gone.md": "v1"},
        max_files=2,
    )
    assert not result.exhaustive
    assert result.removals == ()
    assert result.deferred == ("c.md",)


def test_the_bound_takes_files_in_a_stable_order():
    """Raising the bound must reach the rest, not reshuffle what a run carries."""
    first = plan(file("c.md"), file("a.md"), file("b.md"), max_files=2)
    second = plan(file("b.md"), file("c.md"), file("a.md"), max_files=2)
    assert [write.source_key for write in first.writes] == ["a.md", "b.md"]
    assert [write.source_key for write in second.writes] == ["a.md", "b.md"]


def test_a_file_excluded_by_the_selection_is_never_considered():
    result = plan(file("a.md"), file("notes.txt"))
    assert [write.source_key for write in result.writes] == ["a.md"]
    assert result.selected == 1


def test_exclude_wins_over_include():
    result = plan(file("a.md"), file("draft/b.md"), exclude="draft/**")
    assert [write.source_key for write in result.writes] == ["a.md"]


def test_taking_other_extensions_is_a_pattern_not_a_change_here():
    """Markdown is only the default. The comma form is tested end to end in
    test_settings, which is where a form field becomes a selection."""
    result = plan_run(
        Inventory(files=(file("a.md"), file("b.pdf"), file("c.txt"))),
        previous={},
        selection=Selection(include=("**/*.md", "**/*.pdf")),
        max_files=None,
        max_file_bytes=1_000,
    )
    assert sorted(write.source_key for write in result.writes) == ["a.md", "b.pdf"]


def test_a_file_past_the_size_bound_is_skipped_and_never_removed():
    """A limit of ours is not a statement about the share."""
    result = plan(
        file("big.md", size=5_000),
        previous={"big.md": "v0"},
        max_file_bytes=1_000,
    )
    assert [skip.source_key for skip in result.skips] == ["big.md"]
    assert result.writes == ()
    assert result.removals == ()


def test_a_path_too_long_for_the_platform_is_skipped_before_it_is_refused():
    result = plan(file("x" * (MAX_SOURCE_KEY_LENGTH + 1) + ".md"))
    assert [skip.reason for skip in result.skips] == ["path_too_long"]


def test_a_file_with_no_version_at_all_is_always_written():
    result = plan(file("a.md", version=""), previous={"a.md": ""})
    assert [write.source_key for write in result.writes] == ["a.md"]
    assert result.versionless == 1
