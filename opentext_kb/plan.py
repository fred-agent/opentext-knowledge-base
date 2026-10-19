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
What one run should do, decided before anything is written or fetched.

Nothing here touches Content Server or Fred, which is what lets every rule below be
tested on its own — above all the two that decide whether a team keeps its
documents: when an absence means a deletion, and when a file this run cannot
carry must be left exactly as it is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from opentext_kb.settings import Selection
from opentext_kb.source import Inventory, RemoteFile

# Bounds the platform enforces on what addresses a document. Refusing here
# turns a write rejected mid-run into a named skip decided before the run
# starts.
MAX_SOURCE_KEY_LENGTH = 512
MAX_PATH_SEGMENT_LENGTH = 255


@dataclass(frozen=True, slots=True)
class Write:
    """One document to put in the library.

    The key is the file's path in Content Server and never changes meaning. The
    version is Content Server's own, so Fred can be told what it is holding without
    anyone hashing anything.
    """

    file: RemoteFile

    @property
    def source_key(self) -> str:
        return self.file.path

    @property
    def version(self) -> str:
        return self.file.version


@dataclass(frozen=True, slots=True)
class Removal:
    """One document to take out of the library, because Content Server dropped it."""

    source_key: str


@dataclass(frozen=True, slots=True)
class Skip:
    """One file this run will not carry, and why. Never a removal."""

    source_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    """Everything this run intends, before any of it has happened."""

    writes: tuple[Write, ...] = ()
    unchanged: tuple[str, ...] = ()
    removals: tuple[Removal, ...] = ()
    skips: tuple[Skip, ...] = ()
    exhaustive: bool = False
    selected: int = 0
    versionless: int = 0
    # Paths the bound left out of this run. Named so a run can say so: a file
    # this run never looked at has not been deleted.
    deferred: tuple[str, ...] = field(default=())
    # Content Server listed nothing while the library holds documents.
    source_empty: bool = False


def plan_run(
    inventory: Inventory,
    *,
    previous: Mapping[str, str | None],
    selection: Selection,
    max_files: int | None,
    max_file_bytes: int,
) -> Plan:
    """Decide this run's writes, removals and skips.

    `previous` is what the library itself says it holds, so a document that
    never finished being ingested is absent from it and is written again here.
    Removals are issued only by a run that saw the whole folder. A bounded walk
    proves nothing about what it did not reach, and deducing a deletion from an
    absence it never observed is how a synchronizer quietly empties a library.

    An empty folder facing a populated library proves nothing either: a renamed
    folder, a revoked right or a wrong id all list as empty, and reading that
    as "everything was deleted" would empty the library on a configuration
    mistake. A team that really empties its folder deletes the base instead.
    """
    selected = sorted(
        (entry for entry in inventory.files if selection.selects(entry.path)),
        key=lambda entry: entry.path,
    )

    deferred: tuple[str, ...] = ()
    exhaustive = inventory.exhaustive
    source_empty = not inventory.files and bool(previous)
    if source_empty:
        exhaustive = False
    if max_files is not None and len(selected) > max_files:
        # Deterministic by path, so raising the bound reaches the rest rather
        # than reshuffling which files a run happens to carry.
        deferred = tuple(entry.path for entry in selected[max_files:])
        selected = selected[:max_files]
        exhaustive = False

    writes: list[Write] = []
    unchanged: list[str] = []
    skips: list[Skip] = []
    versionless = 0

    for entry in selected:
        reason = _unusable(entry, max_file_bytes)
        if reason is not None:
            skips.append(Skip(entry.path, reason))
            continue
        if not entry.version:
            # Content Server gave no modification date, so nothing here can tell an
            # edited file from an untouched one.
            versionless += 1
            writes.append(Write(entry))
            continue
        if previous.get(entry.path) == entry.version:
            unchanged.append(entry.path)
            continue
        writes.append(Write(entry))

    seen = {entry.path for entry in selected}
    removals = (
        tuple(Removal(key) for key in sorted(set(previous) - seen))
        if exhaustive
        else ()
    )

    return Plan(
        writes=tuple(writes),
        unchanged=tuple(unchanged),
        removals=removals,
        skips=tuple(skips),
        exhaustive=exhaustive,
        selected=len(selected),
        versionless=versionless,
        deferred=deferred,
        source_empty=source_empty,
    )


def _unusable(entry: RemoteFile, max_file_bytes: int) -> str | None:
    """Why this file cannot be a document, or None if it can.

    A skip never removes. The library loses a document because Content Server
    dropped it, never because this implementation could not carry it — deleting
    a team's document over our own size bound would be the worse mistake by far.
    """
    if entry.size_bytes > max_file_bytes:
        return "too_large"
    if len(entry.path) > MAX_SOURCE_KEY_LENGTH:
        return "path_too_long"
    if any(len(part) > MAX_PATH_SEGMENT_LENGTH for part in entry.path.split("/")):
        return "path_too_long"
    return None


__all__ = [
    "MAX_PATH_SEGMENT_LENGTH",
    "MAX_SOURCE_KEY_LENGTH",
    "Plan",
    "Removal",
    "Skip",
    "Write",
    "plan_run",
]
