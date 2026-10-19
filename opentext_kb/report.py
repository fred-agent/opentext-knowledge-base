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
What a run actually did, in the terms this implementation can prove.

Deliberately not Fred's vocabulary: its result shape is one translation away,
in the layer that talks to it, so a change on either side is a change to one
file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Past this many, an operator is reading a failure, not a list. The platform
# bounds what it stores too; stopping here keeps the two from disagreeing.
MAX_ISSUES = 50


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str = ""
    subject: str | None = None


@dataclass
class RunReport:
    """Counts what happened, and whether an absence was allowed to mean anything."""

    exhaustive: bool = False
    discovered: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    # Accepted by Fred and still being ingested when this run stopped waiting.
    # Neither created nor updated: this run cannot prove either.
    pending: int = 0
    published_bytes: int = 0
    warnings: list[Issue] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)

    def wrote(self, *, created: bool, size_bytes: int) -> None:
        if created:
            self.created += 1
        else:
            self.updated += 1
        self.published_bytes += size_bytes

    def skip(self, source_key: str, reason: str, message: str = "") -> None:
        """One document this run will not carry, and why. Never an error.

        A skip leaves whatever the library already held: a limit of ours is not
        a statement about Content Server.
        """
        self.skipped += 1
        self.warn(reason, message, subject=source_key)

    def still_ingesting(self, source_key: str, message: str = "") -> None:
        """One document Fred is still ingesting. Never an error.

        The library lists it with its version, so the next run leaves it be.
        """
        self.pending += 1
        self.warn("ingestion_pending", message, subject=source_key)

    def warn(self, code: str, message: str = "", subject: str | None = None) -> None:
        _add(self.warnings, Issue(code, message, subject))

    def fail(self, code: str, message: str = "", subject: str | None = None) -> None:
        _add(self.errors, Issue(code, message, subject))

    @property
    def succeeded(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"{self.discovered} discovered",
            f"{self.created} created",
            f"{self.updated} updated",
            f"{self.removed} removed",
            f"{self.unchanged} unchanged",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.pending:
            parts.append(f"{self.pending} still ingesting")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        if not self.exhaustive:
            parts.append("partial pass")
        return ", ".join(parts)

    def metrics(self) -> dict[str, object]:
        return {
            "exhaustive": self.exhaustive,
            "skipped": self.skipped,
            "pending": self.pending,
            "published_bytes": self.published_bytes,
        }


def _add(issues: list[Issue], issue: Issue) -> None:
    """Stop collecting at the bound, and say once that collecting stopped."""
    if len(issues) < MAX_ISSUES:
        issues.append(issue)
    elif len(issues) == MAX_ISSUES:
        issues.append(Issue("issues_truncated", f"more than {MAX_ISSUES} reported"))
