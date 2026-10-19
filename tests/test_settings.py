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
"""What a team fills in, and what this refuses to guess at."""

from __future__ import annotations

import pytest

from opentext_kb.settings import (
    DEFAULT_MAX_FILES,
    ConfigurationError,
    read_settings,
)

REQUIRED = {
    "folder_id": "123456789",
    "account": "team-account",
    "password": "team-password",  # pragma: allowlist secret
}


def settings(**overrides):
    return read_settings({**REQUIRED, **overrides})


def test_a_folder_and_an_account_are_all_that_is_required():
    result = settings()
    assert result.folder_id == "123456789"
    assert result.account.name == "team-account"
    assert result.max_files == DEFAULT_MAX_FILES


def test_the_default_selection_takes_common_documents_and_leaves_the_rest():
    selection = settings().selection
    for path in ("a.pdf", "notes/b.docx", "c.pptx", "d.md", "e.txt"):
        assert selection.selects(path), path
    for path in ("image.png", "archive.zip", "macro.xlsm"):
        assert not selection.selects(path), path


def test_the_selection_ignores_case_on_both_sides():
    """Server is full of `.PDF`; a pattern typed in capitals means the same."""
    assert settings().selection.selects("RAPPORT.PDF")
    assert settings(include="**/*.XLSM").selection.selects("pps/4001001.xlsm")


@pytest.mark.parametrize(
    "folder_id", ["", "abc", "12 34", "https://server/nodes/12", None]
)
def test_a_folder_id_that_is_not_digits_is_refused(folder_id: object):
    with pytest.raises(ConfigurationError) as error:
        settings(folder_id=folder_id)
    assert error.value.code == "folder_id_invalid"


@pytest.mark.parametrize("missing", ["account", "password"])
def test_an_account_needs_both_its_name_and_its_password(missing: str):
    with pytest.raises(ConfigurationError) as error:
        settings(**{missing: ""})
    assert error.value.code == "account_missing"


def test_several_extensions_are_written_with_commas():
    result = settings(include="**/*.md, **/*.pdf")
    assert result.selection.selects("a.md")
    assert result.selection.selects("reports/b.pdf")
    assert not result.selection.selects("c.txt")


def test_several_patterns_may_also_be_written_on_separate_lines():
    result = settings(include="**/*.md\n**/*.pdf")
    assert result.selection.selects("reports/b.pdf")


def test_exclude_wins_over_include():
    result = settings(include="**/*.md", exclude="Brouillons/**")
    assert result.selection.selects("a.md")
    assert not result.selection.selects("brouillons/a.md")


def test_a_negated_pattern_is_refused_rather_than_quietly_reordered():
    with pytest.raises(ConfigurationError) as error:
        settings(exclude="!keep.md")
    assert error.value.code == "selection_invalid"


def test_a_cleared_field_means_its_default_rather_than_a_broken_instance():
    result = settings(include="", exclude="")
    assert result.selection.selects("a.pdf")


def test_a_bound_cleared_by_an_operator_means_no_bound():
    assert settings(max_files=None).max_files is None


@pytest.mark.parametrize("value", [0, -1, "20", True])
def test_a_bound_that_is_not_a_positive_whole_number_is_refused(value: object):
    with pytest.raises(ConfigurationError) as error:
        settings(max_files=value)
    assert error.value.code == "max_files_invalid"


def test_a_text_field_arriving_as_something_else_is_refused():
    with pytest.raises(ConfigurationError) as error:
        settings(include=42)
    assert error.value.code == "include_invalid"


def test_accepting_any_certificate_is_off_unless_asked_for():
    assert not settings().trust_any_certificate
    assert settings(trust_any_certificate=True).trust_any_certificate


def test_the_certificate_flag_must_be_a_boolean():
    with pytest.raises(ConfigurationError) as error:
        settings(trust_any_certificate="yes")
    assert error.value.code == "trust_any_certificate_invalid"


@pytest.mark.parametrize("profile", ["fast", "medium", "rich"])
def test_the_ingestion_profile_is_read(profile: str):
    assert settings(profile=profile).profile == profile


@pytest.mark.parametrize("configuration", [{}, {"profile": ""}, {"profile": None}])
def test_an_omitted_or_cleared_profile_defaults_to_medium(configuration):
    assert settings(**configuration).profile == "medium"


@pytest.mark.parametrize("profile", ["turbo", "MEDIUM", 42])
def test_an_invalid_profile_is_refused(profile):
    with pytest.raises(ConfigurationError) as error:
        settings(profile=profile)
    assert error.value.code == "profile_invalid"
