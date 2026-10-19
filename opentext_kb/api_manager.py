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
Thales's API Manager, a gateway that may stand in front of Content Server
(`gateway: thales-api-manager`).

Not Content Server's protocol: a gateway's, which wants a key, a user and an
HMAC signature over every request. A deployment reaching Content Server
directly has no use for it (`gateway: none`). The signature is the one this
gateway has accepted in production; a test pins it to a reference value.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

HASHES: Mapping[str, Callable[[], Any]] = {
    "SHA-256": hashlib.sha256,
    "SHA-384": hashlib.sha384,
    "SHA-512": hashlib.sha512,
}


@dataclass(frozen=True, slots=True)
class Subscription:
    """The API Manager's identity for this deployment: who calls, not what they may read."""

    api_key: str
    api_secret: str
    username: str
    hash_algorithm: str = "SHA-512"


class ApiManager:
    """Signs every request for the API Manager, at the moment it is sent."""

    def __init__(
        self,
        subscription: Subscription,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._subscription = subscription
        self._clock = clock

    def headers(self, method: str, url: str) -> dict[str, str]:
        # Signed per attempt: the API Manager refuses a stale timestamp.
        return signed_headers(
            method, url, subscription=self._subscription, now=self._clock()
        )


def canonical_resource(url: str) -> str:
    """The part of a URL the API Manager signs: path, then the query re-encoded.

    The encoding is the API Manager's, not RFC 3986's: values fully
    percent-encoded, and five characters encoded on top that `quote` leaves.
    """
    split = urlsplit(url)
    resource = split.path
    pairs = parse_qsl(split.query, keep_blank_values=True)
    if pairs:
        resource += "?" + "&".join(
            f"{key}={quote(value, safe='')}" for key, value in pairs
        )
    for character, encoded in (
        ("'", "%27"),
        ("!", "%21"),
        ("~", "%7E"),
        ("(", "%28"),
        (")", "%29"),
    ):
        resource = resource.replace(character, encoded)
    return resource


def signed_headers(
    method: str, url: str, *, subscription: Subscription, now: datetime
) -> dict[str, str]:
    """The API Manager's headers for one request, signed at `now`.

    The timestamp is UTC. The gateway's reference client mixes the local date
    with the UTC hour; in a pod, whose clock is UTC, the two agree.
    """
    digest = HASHES.get(subscription.hash_algorithm.upper())
    if digest is None:
        raise ValueError(
            f"hash algorithm {subscription.hash_algorithm!r} is not one of {sorted(HASHES)}"
        )
    timestamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000")
    message = (
        f"Resource: {canonical_resource(url)}\nVerb: {method.upper()}\n"
        f"Timestamp: {timestamp}\nAlgorithm: {subscription.hash_algorithm}"
    )
    signature = hmac.new(
        subscription.api_secret.encode("utf-8"), message.encode("utf-8"), digest
    ).digest()
    return {
        "apikey": subscription.api_key,
        "username": subscription.username,
        "Content-Type": "application/json",
        "X-Apim-Hash-Algorithm": subscription.hash_algorithm,
        "apisecret": base64.b64encode(signature).decode("ascii"),
        "X-Apim-Timestamp": timestamp,
    }
