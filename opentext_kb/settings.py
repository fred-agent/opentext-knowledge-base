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
What a team fills in, read into what this implementation works with.

Values are read, never coerced: a number arriving as text is a configuration
that was not validated, and quietly accepting it hides the fault one layer
further from whoever can fix it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

import pathspec

from opentext_kb.content_server import Account

# The documents Knowledge Flow turns into text worth searching. Anything else
# is the team's choice to add, in this field.
DEFAULT_INCLUDE = "**/*.pdf,**/*.docx,**/*.pptx,**/*.md,**/*.txt"

DEFAULT_MAX_FILES = 2000
IngestionProfile = Literal["fast", "medium", "rich"]
INGESTION_PROFILES = ("fast", "medium", "rich")
DEFAULT_PROFILE: IngestionProfile = "medium"


class ConfigurationError(ValueError):
    """A configured value this implementation cannot work with."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PatternError(ValueError):
    """A pattern this implementation will not work with."""


class Selection:
    """The rule deciding whether a path in Content Server belongs in the library.

    gitignore's patterns, matched without regard to case: Content Server is full of
    `.PDF` and `.XLSM`, and a team writing `**/*.pdf` means those too.
    """

    def __init__(
        self,
        *,
        include: Sequence[str] = (),
        exclude: Sequence[str] = (),
    ) -> None:
        self.include = tuple(include) or tuple(_patterns(DEFAULT_INCLUDE))
        self.exclude = tuple(exclude)
        self._include = _compile(self.include, "include")
        self._exclude = _compile(self.exclude, "exclude") if self.exclude else None

    def selects(self, path: str) -> bool:
        folded = path.casefold()
        if self._exclude is not None and self._exclude.match_file(folded):
            return False
        return self._include.match_file(folded)


@dataclass(frozen=True, slots=True)
class Settings:
    folder_id: str
    account: Account
    trust_any_certificate: bool
    selection: Selection
    max_files: int | None
    profile: IngestionProfile = DEFAULT_PROFILE


def read_settings(configuration: Mapping[str, object]) -> Settings:
    """Read one instance's configuration, or say which field is wrong."""
    folder_id = _text(configuration, "folder_id")
    if not folder_id.isdigit():
        raise ConfigurationError(
            "folder_id_invalid",
            "folder_id is the number Content Server shows for the folder, digits only.",
        )

    account = _text(configuration, "account")
    password = _text(configuration, "password")
    if not account or not password:
        raise ConfigurationError(
            "account_missing",
            "A Content Server account and its password are both required.",
        )

    try:
        selection = Selection(
            include=_patterns(_text(configuration, "include") or DEFAULT_INCLUDE),
            exclude=_patterns(_text(configuration, "exclude")),
        )
    except PatternError as error:
        raise ConfigurationError("selection_invalid", str(error)) from error

    profile = _text(configuration, "profile") or DEFAULT_PROFILE
    if profile not in INGESTION_PROFILES:
        raise ConfigurationError(
            "profile_invalid", "profile must be fast, medium, or rich"
        )

    return Settings(
        folder_id=folder_id,
        account=Account(name=account, password=password),
        # Never refused here: a closed network with a private certificate
        # authority is a real situation. The run reports it instead, every time.
        trust_any_certificate=_flag(configuration, "trust_any_certificate"),
        selection=selection,
        max_files=_bound(configuration, "max_files", DEFAULT_MAX_FILES),
        profile=cast(IngestionProfile, profile),
    )


def _patterns(raw: str) -> tuple[str, ...]:
    """Read a form field holding several patterns, by comma or by line."""
    separated = raw.replace("\n", ",").split(",")
    return tuple(pattern.strip() for pattern in separated if pattern.strip())


def _compile(values: Sequence[str], label: str) -> pathspec.PathSpec:
    for value in values:
        # A negated pattern quietly re-includes what an exclusion removed, in an
        # order nobody reading two separate fields can see.
        if value.startswith("!"):
            raise PatternError(
                f"{label} pattern {value!r} negates; put it in the other field instead."
            )
    try:
        return pathspec.PathSpec.from_lines(
            "gitignore", [value.casefold() for value in values]
        )
    except Exception as error:
        raise PatternError(f"{label} pattern is not valid: {error}") from error


def _text(configuration: Mapping[str, object], key: str) -> str:
    """Read an optional text field, which a cleared form sends as empty."""
    raw = configuration.get(key, "")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ConfigurationError(f"{key}_invalid", f"{key} must be text, not {raw!r}")
    return raw.strip()


def _flag(configuration: Mapping[str, object], key: str) -> bool:
    raw = configuration.get(key, False)
    if raw is None or raw == "":
        return False
    if not isinstance(raw, bool):
        raise ConfigurationError(
            f"{key}_invalid", f"{key} must be true or false, not {raw!r}"
        )
    return raw


def _bound(configuration: Mapping[str, object], key: str, default: int) -> int | None:
    """Read a positive whole number, or None when an operator cleared it."""
    if key not in configuration:
        return default
    raw = configuration[key]
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ConfigurationError(
            f"{key}_invalid", f"{key} must be a positive whole number, not {raw!r}"
        )
    return raw
