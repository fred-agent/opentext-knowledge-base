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
The fake's behaviour, free of any server: one request in, one answer out.

The tree is read from disk on every request, so adding, editing or deleting a
file under the root is what a test of a synchronization does. A node id is
derived from the path, so it stays the same from one request to the next and
from one start to the next — as a Content Server id does.

On disk, `∕` (U+2215, division slash) stands for a `/` inside a Content Server name,
which no file system can hold.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

NAME_SLASH_ON_DISK = "∕"
# The API Manager refuses a signature older than this; so does the fake.
MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Credentials:
    """What the fake accepts: accounts, and the API Manager subscription if any.

    Several accounts, as several teams have, each reading with its own; the
    fake gives them all the same rights. Without an `api_key` there is no
    gateway in front, and no gateway header is asked for.
    """

    account: str
    password: str
    api_key: str = ""
    api_secret: str = ""
    username: str = ""
    other_accounts: tuple[tuple[str, str], ...] = ()

    def accepts(self, authorization: str | None) -> bool:
        accounts = ((self.account, self.password), *self.other_accounts)
        return any(
            authorization
            == "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()
            for name, password in accounts
        )


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: bytes = b""
    content_type: str = "application/json"


class FakeContentServer:
    """A local folder answering as Content Server answers."""

    def __init__(self, root: Path, *, root_id: str, credentials: Credentials) -> None:
        self._root = root
        self._root_id = root_id
        self._credentials = credentials

    def respond(self, method: str, url: str, headers: dict[str, str]) -> Answer:
        """Answer one request, `headers` with lower-case names."""
        refusal = self._refusal(method, url, headers)
        if refusal is not None:
            return refusal
        split = urlsplit(url)
        parts = split.path.rstrip("/").split("/")
        if method != "GET" or len(parts) < 3 or parts[-3] != "nodes":
            return _json(404, {"error": "no such route"})
        node_id, verb = parts[-2], parts[-1]
        target = self._resolve(node_id)
        if target is None:
            return _json(404, {"error": f"node {node_id} not found"})
        if verb == "nodes":
            query = parse_qs(split.query)
            return self._list(target, query)
        if verb == "content":
            if not target.is_file():
                return _json(400, {"error": "a folder has no content"})
            return Answer(200, target.read_bytes(), "application/octet-stream")
        return _json(404, {"error": "no such route"})

    def node_id(self, relative: str) -> str:
        """The id this fake gives the node at `relative` (`""` for the root)."""
        if not relative:
            return self._root_id
        digest = hashlib.sha256(relative.encode("utf-8")).digest()
        return str(10**9 + int.from_bytes(digest[:6]) % (9 * 10**9))

    # ── internals ─────────────────────────────────────────────────────────────

    def _refusal(self, method: str, url: str, headers: dict[str, str]) -> Answer | None:
        if not self._credentials.accepts(headers.get("authorization")):
            return _json(401, {"error": "account refused"})
        if not self._credentials.api_key:
            return None
        if headers.get("apikey") != self._credentials.api_key:
            return _json(401, {"error": "unknown API key"})
        if headers.get("username") != self._credentials.username:
            return _json(401, {"error": "unknown API Manager user"})
        timestamp = headers.get("x-apim-timestamp", "")
        algorithm = headers.get("x-apim-hash-algorithm", "")
        try:
            signed_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.000").replace(
                tzinfo=UTC
            )
        except ValueError:
            return _json(401, {"error": "timestamp unreadable"})
        if abs(datetime.now(UTC) - signed_at) > MAX_CLOCK_SKEW:
            return _json(401, {"error": "timestamp too far from now"})
        if headers.get("apisecret") != _gateway_signature(
            method, url, timestamp, algorithm, self._credentials.api_secret
        ):
            return _json(401, {"error": "signature does not match"})
        return None

    def _resolve(self, node_id: str) -> Path | None:
        if node_id == self._root_id:
            return self._root
        for path in self._root.rglob("*"):
            relative = path.relative_to(self._root).as_posix()
            if self.node_id(relative) == node_id:
                return path
        return None

    def _list(self, folder: Path, query: dict[str, list[str]]) -> Answer:
        if not folder.is_dir():
            # What Content Server says of a document asked for its children.
            return _json(200, {"error": "not a container"})
        limit = int(query.get("limit", ["500"])[0])
        page = int(query.get("page", ["1"])[0])
        children = sorted(
            path for path in folder.iterdir() if not path.name.startswith(".")
        )
        page_total = max(1, math.ceil(len(children) / limit))
        shown = children[(page - 1) * limit : page * limit]
        return _json(
            200,
            {
                "collection": {"paging": {"page": page, "page_total": page_total}},
                "results": [
                    {"data": {"properties": self._properties(path)}} for path in shown
                ],
            },
        )

    def _properties(self, path: Path) -> dict[str, object]:
        relative = path.relative_to(self._root).as_posix()
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).strftime(
            "%Y-%m-%dT%H:%M:%S+0000"
        )
        name = path.name.replace(NAME_SLASH_ON_DISK, "/")
        if path.is_dir():
            # Content Server's own shape: subtype 0, flagged as a container.
            return {
                "id": int(self.node_id(relative)),
                "name": name,
                "type": 0,
                "container": True,
                "modify_date": modified,
            }
        return {
            "id": int(self.node_id(relative)),
            "name": name,
            "type": 144,
            "container": False,
            "size": stat.st_size,
            "modify_date": modified,
        }


def _gateway_signature(
    method: str, url: str, timestamp: str, algorithm: str, secret: str
) -> str:
    """The gateway's signature, as its reference client computes it."""
    split = urlsplit(url)
    resource = split.path
    if split.query:
        params = parse_qs(split.query, keep_blank_values=True)
        resource += "?" + "&".join(
            f"{key}={quote(str(value), safe='')}"
            for key, values in params.items()
            for value in values
        )
    for character, encoded in (
        ("'", "%27"),
        ("!", "%21"),
        ("~", "%7E"),
        ("(", "%28"),
        (")", "%29"),
    ):
        resource = resource.replace(character, encoded)
    message = f"Resource: {resource}\nVerb: {method.upper()}\nTimestamp: {timestamp}\nAlgorithm: {algorithm}"
    constructor = (
        hashlib.sha256
        if "256" in algorithm
        else hashlib.sha384
        if "384" in algorithm
        else hashlib.sha512
    )
    return base64.b64encode(
        hmac.new(secret.encode(), message.encode(), constructor).digest()
    ).decode()


def _json(status: int, payload: object) -> Answer:
    return Answer(status, json.dumps(payload).encode("utf-8"))
