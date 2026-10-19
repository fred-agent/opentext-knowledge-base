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
Reading OpenText Content Server: one walk of a folder tree, and one document at
a time.

Two calls of its REST API (v2) are used, the two proven in production against a
Content Server behind a gateway: `GET /nodes/{id}/nodes` lists a folder
page by page, `GET /nodes/{id}/content` fetches a document. The account (HTTP
Basic) decides what the walk can see. A gateway in front, such as Thales's API
Manager (`api_manager.py`), adds its own headers to every request.

Full rationale is in README.md; the comments below say only what the code cannot.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Coroutine, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from opentext_kb.source import FileTooLarge, Inventory, RemoteFile, SourceUnavailable
from opentext_kb.tls import certificate_rejected, not_trusted

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 500

# A `/` inside a name is a character to Content Server and a folder separator to
# Fred. The fullwidth solidus reads the same and separates nothing, so the
# library shows the name as Content Server does without inventing a folder.
SLASH_IN_NAME = "／"

# Bounds on the walk itself, so a broken answer costs one bounded run rather
# than a pod that never returns. A walk that hits one is not exhaustive.
MAX_FOLDERS = 20_000
MAX_DEPTH = 32

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0
# 500 included: a gateway answers it for a backend that is momentarily gone,
# and a year of retries against one in production says it passes.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0
_MAX_DETAIL = 200


@dataclass(frozen=True, slots=True)
class Account:
    """The Content Server account a team reads with. Its rights bound what the walk sees."""

    name: str
    password: str


class NotAFolder(SourceUnavailable):
    """The configured node answered, but not as a folder Content Server can list."""


class Gateway(Protocol):
    """Something in front of Content Server that wants its own headers on every request."""

    def headers(self, method: str, url: str) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class _Node:
    """One child of a folder, as the listing described it."""

    node_id: str
    name: str
    is_folder: bool
    size_bytes: int
    modified: str


class ContentServerSource:
    """One Content Server folder, read over HTTP with no state kept between runs."""

    def __init__(
        self,
        *,
        base_url: str,
        folder_id: str,
        account: Account,
        max_file_bytes: int,
        gateway: Gateway | None = None,
        verify: ssl.SSLContext | bool = True,
        page_size: int = DEFAULT_PAGE_SIZE,
        listing_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._folder_id = folder_id
        self._gateway = gateway
        self._max_file_bytes = max_file_bytes
        self._base_url = base_url.rstrip("/")
        self._page_size = page_size
        self._listing_concurrency = listing_concurrency
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
            verify=verify,
            # Never followed: a redirect off this host would present the
            # account's password to whatever answered.
            follow_redirects=False,
            auth=httpx.BasicAuth(account.name, account.password),
            headers={"User-Agent": "opentext-knowledge-base"},
        )

    async def inventory(self) -> Inventory:
        """Walk the tree level by level, a few folders listed at once.

        Unbounded in files for the reason the WebDAV walk is: only the
        selection knows which files are documents, and a walk cut short by a
        count of files could never notice a deletion again. Folders and depth
        stay bounded, because those end a walk that has no end of its own.
        """
        found: dict[str, RemoteFile] = {}
        visited: set[str] = {self._folder_id}
        level: list[tuple[str, str]] = [(self._folder_id, "")]
        depth = 0
        exhaustive = True
        permits = asyncio.Semaphore(self._listing_concurrency)

        async def listed(node_id: str, prefix: str) -> tuple[str, list[_Node]]:
            async with permits:
                return prefix, await self._children(node_id)

        while level:
            listings = await _all(listed(node_id, prefix) for node_id, prefix in level)
            following: list[tuple[str, str]] = []
            for prefix, children in listings:
                for child in children:
                    segment = _segment(child.name)
                    if segment is None:
                        logger.warning(
                            "[OPENTEXT KB] node %s has a name no library can hold: %r",
                            child.node_id,
                            child.name,
                        )
                        continue
                    path = f"{prefix}{segment}"
                    if not child.is_folder:
                        found[path] = RemoteFile(
                            path=path,
                            version=f"mtime:{child.modified}:{child.size_bytes}"
                            if child.modified
                            else "",
                            size_bytes=child.size_bytes,
                            ref=child.node_id,
                        )
                    elif child.node_id not in visited:
                        if depth + 1 > MAX_DEPTH or len(visited) >= MAX_FOLDERS:
                            exhaustive = False
                            continue
                        visited.add(child.node_id)
                        following.append((child.node_id, f"{path}/"))
            level = following
            depth += 1

        if not exhaustive:
            logger.warning(
                "[OPENTEXT KB] folder %s: the walk was cut short, so nothing it did "
                "not see counts as deleted",
                self._folder_id,
            )
        return Inventory(files=tuple(found.values()), exhaustive=exhaustive)

    async def read(self, file: RemoteFile) -> bytes:
        """Fetch one document, refusing more bytes than were agreed."""
        url = f"{self._base_url}/nodes/{file.ref}/content"
        what = f"reading {file.path}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with self._client.stream(
                    "GET", url, headers=self._sign("GET", url)
                ) as response:
                    if (
                        response.status_code in _RETRYABLE_STATUS
                        and attempt < MAX_ATTEMPTS
                    ):
                        await _pause(attempt, response.headers.get("Retry-After"))
                        continue
                    if response.status_code != 200:
                        await response.aread()
                        _raise_for(response.status_code, response.text, what)
                    return await self._collect(response, file)
            except httpx.TransportError as error:
                self._raise_transport(error, what, attempt)
                await _pause(attempt, None)
        raise SourceUnavailable(f"{what}: {MAX_ATTEMPTS} attempts failed")

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── internals ─────────────────────────────────────────────────────────────

    def _sign(self, method: str, url: str) -> dict[str, str]:
        return self._gateway.headers(method, url) if self._gateway is not None else {}

    async def _children(self, node_id: str) -> list[_Node]:
        """Every child of one folder, following Content Server's pages to the last."""
        children: list[_Node] = []
        page = 1
        while True:
            # The URL is written out rather than built from params, so the
            # request sent is exactly the one that was signed.
            url = f"{self._base_url}/nodes/{node_id}/nodes?limit={self._page_size}&page={page}"
            answer = await self._get_json(url, f"listing folder {node_id}")
            results = answer.get("results")
            if not isinstance(results, list):
                raise NotAFolder(
                    f"listing folder {node_id}: the answer holds no `results` list — "
                    "this node is probably not a folder"
                )
            children.extend(node for node in map(_node, results) if node is not None)
            if page >= _page_total(answer):
                return children
            page += 1

    async def _get_json(self, url: str, what: str) -> dict[str, Any]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(url, headers=self._sign("GET", url))
            except httpx.TransportError as error:
                self._raise_transport(error, what, attempt)
                await _pause(attempt, None)
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                await _pause(attempt, response.headers.get("Retry-After"))
                continue
            _raise_for(response.status_code, response.text, what)
            try:
                answer = response.json()
            except ValueError as error:
                raise SourceUnavailable(f"{what}: the answer is not JSON") from error
            if not isinstance(answer, dict):
                raise SourceUnavailable(f"{what}: the answer is not a JSON object")
            return answer
        raise SourceUnavailable(f"{what}: {MAX_ATTEMPTS} attempts failed")

    def _raise_transport(
        self, error: httpx.TransportError, what: str, attempt: int
    ) -> None:
        rejected = certificate_rejected(error)
        if rejected is not None:
            # Never retried: a trust store does not change between two attempts.
            raise not_trusted(rejected, what) from error
        if attempt >= MAX_ATTEMPTS:
            raise SourceUnavailable(f"{what}: {error}") from error

    async def _collect(self, response: httpx.Response, file: RemoteFile) -> bytes:
        # Checked while reading: the listed size is Content Server's claim about a file
        # it has since been free to change.
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            if len(buffer) + len(chunk) > self._max_file_bytes:
                raise FileTooLarge(
                    f"{file.path} is past the {self._max_file_bytes} byte bound"
                )
            buffer.extend(chunk)
        return bytes(buffer)


async def _all[T](awaitables: Iterable[Coroutine[Any, Any, T]]) -> list[T]:
    """Run them together; on the first failure cancel the rest and raise it as is."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(awaitable) for awaitable in awaitables]
    except* SourceUnavailable as failures:
        raise failures.exceptions[0] from None
    return [task.result() for task in tasks]


def _node(item: object) -> _Node | None:
    """One listing entry, or None when it is not one this walk can use."""
    data = item.get("data") if isinstance(item, dict) else None
    properties = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(properties, dict):
        return None
    node_id = str(properties.get("id", "")).strip()
    name = properties.get("name")
    if not node_id.isdigit() or not isinstance(name, str):
        # A node id lands in a URL that is then signed: anything but digits is
        # refused rather than escaped.
        return None
    size = properties.get("size")
    modified = properties.get("modify_date")
    return _Node(
        node_id=node_id,
        name=name,
        is_folder=_is_folder(properties),
        size_bytes=size if isinstance(size, int) and size >= 0 else 0,
        modified=modified.strip() if isinstance(modified, str) else "",
    )


def _is_folder(properties: Mapping[str, Any]) -> bool:
    # The rule proven in production, unchanged.
    if properties.get("is_folder") is True or properties.get("container") is True:
        return True
    return str(properties.get("type", "")).lower() in {"folder", "directory"}


def _page_total(answer: Mapping[str, Any]) -> int:
    collection = answer.get("collection")
    paging = collection.get("paging") if isinstance(collection, dict) else None
    raw = paging.get("page_total", 1) if isinstance(paging, dict) else 1
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _segment(name: str) -> str | None:
    """A Content Server name as one path segment of a library key, or None if it cannot be one."""
    segment = name.strip().replace("/", SLASH_IN_NAME)
    if segment in ("", ".", ".."):
        return None
    if any(character < " " or character == "\x7f" for character in segment):
        return None
    return segment


async def _pause(attempt: int, retry_after: str | None) -> None:
    """Wait before trying again, honouring the server's own answer if it gave one."""
    delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
    if retry_after and retry_after.strip().isdigit():
        delay = min(float(retry_after.strip()), MAX_RETRY_AFTER_SECONDS)
    await asyncio.sleep(delay)


def _raise_for(status: int, body: str, what: str) -> None:
    if status == 200:
        return
    if 300 <= status < 400:
        raise SourceUnavailable(
            f"{what}: Content Server redirected ({status}). Configure the address it "
            "redirects to, so credentials are never sent elsewhere."
        )
    if status in (401, 403):
        raise SourceUnavailable(
            f"{what}: refused ({status}). Check the Content Server account and password, "
            "and this deployment's gateway credentials, if it has a gateway."
        )
    if status == 404:
        raise NotAFolder(f"{what}: Content Server has no such node (404).")
    detail = " ".join(body.split())[:_MAX_DETAIL]
    raise SourceUnavailable(f"{what}: {status} {detail}")
