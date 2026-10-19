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
What the operator deploying this pod decides, and how the pod stops.

Two kinds of setting, kept apart on purpose. What a *team* chooses — which
folder, read with which Content Server account, which files — is a field in its
form and is read in `settings.py`. What the *operator* decides — which Content
Server, through which gateway, how hard this pod leans on it and on Fred's
ingestion — is the same for every team the pod serves, and lives in
`configuration.yaml` under this pod's own `opentext:` section, plus a gateway's
secrets in the environment.
"""

from __future__ import annotations

import logging
import os
import signal
from types import FrameType
from typing import Literal, cast

from fred_sdk.knowledge_base import MissingPodConfiguration
from fred_sdk.knowledge_base.configuration import PodConfiguration
from pydantic import BaseModel, ConfigDict, Field

from opentext_kb.api_manager import ApiManager, Subscription
from opentext_kb.content_server import DEFAULT_PAGE_SIZE, Gateway

logger = logging.getLogger(__name__)

# Thales's API Manager subscription's two secrets. Variables, never
# configuration: the YAML is a ConfigMap, readable by whoever can read the namespace.
API_KEY_ENV = "OPENTEXT_KB_API_KEY"  # nosec B105  # pragma: allowlist secret
API_SECRET_ENV = "OPENTEXT_KB_API_SECRET"  # nosec B105  # pragma: allowlist secret


class DeploymentIncomplete(RuntimeError):
    """This deployment lacks something every run needs: the address, or a gateway's credentials."""


class ApiManagerSettings(BaseModel):
    """Thales's API Manager, when `gateway: thales-api-manager`."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        default="",
        description="The API Manager user the subscription belongs to (header `username`).",
    )
    hash_algorithm: str = Field(
        default="SHA-512",
        pattern=r"^SHA-(256|384|512)$",
        description="The HMAC the API Manager expects (header `X-Apim-Hash-Algorithm`).",
    )


class OpenTextDeployment(BaseModel):
    """The `opentext:` section of `configuration.yaml`."""

    # A misspelt key is refused rather than ignored: an operator who wrote
    # `concurency: 16` believes it applies.
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(
        default="",
        description="Content Server's REST API root (…/api/v2), or a gateway's address for it.",
    )
    gateway: Literal["none", "thales-api-manager"] = Field(
        default="none",
        description="What stands in front of Content Server and wants its own headers.",
    )
    api_manager: ApiManagerSettings = Field(default_factory=ApiManagerSettings)
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=1000,
        description="Children asked for per listing page.",
    )
    listing_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Folders listed at once during a walk.",
    )
    concurrency: int = Field(
        default=4,
        ge=1,
        le=64,
        description=(
            "Documents one run fetches and hands to Fred at once. Each is held "
            "whole in memory while it is written, so this times max_file_mib is "
            "a run's memory ceiling."
        ),
    )
    max_file_mib: int = Field(
        default=50,
        ge=1,
        le=1024,
        description="A larger document is skipped, never removed.",
    )
    ingestion_wait_seconds: int = Field(
        default=600,
        ge=1,
        description=(
            "How long a run follows one document's ingestion. Past it the "
            "document is reported as still ingesting, not as failed."
        ),
    )

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mib * 1024 * 1024

    def address(self) -> str:
        """Where Content Server is reached, or why this deployment cannot say."""
        if not self.base_url.strip():
            raise DeploymentIncomplete(
                "No Content Server address: set `opentext.base_url` in configuration.yaml."
            )
        return self.base_url.strip()

    def gateway_in_front(self) -> Gateway | None:
        """The gateway every request goes through, built from this pod's environment."""
        if self.gateway == "none":
            return None
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        api_secret = os.environ.get(API_SECRET_ENV, "").strip()
        if not api_key or not api_secret or not self.api_manager.username:
            raise DeploymentIncomplete(
                f"The API Manager subscription is incomplete: set ${API_KEY_ENV}, "
                f"${API_SECRET_ENV} and `opentext.api_manager.username` in configuration.yaml."
            )
        return ApiManager(
            Subscription(
                api_key=api_key,
                api_secret=api_secret,
                username=self.api_manager.username,
                hash_algorithm=self.api_manager.hash_algorithm,
            )
        )


class OpenTextPodConfiguration(PodConfiguration):
    """The SDK's pod configuration, plus this pod's own section."""

    opentext: OpenTextDeployment = Field(default_factory=OpenTextDeployment)


def load_deployment() -> OpenTextPodConfiguration | None:
    """This pod's configuration, or None where there is no Fred at all.

    An invalid file stops the process with the SDK's banner, which is why the
    entry point calls this once before serving anything.
    """
    try:
        # The SDK's loader builds whichever class it is called on; only its
        # annotation names the base class.
        return cast(OpenTextPodConfiguration, OpenTextPodConfiguration.load())
    except MissingPodConfiguration as error:
        logger.info("No Fred configuration (%s): deployment defaults apply", error)
        return None


def stop_on_sigterm() -> None:
    """Make the orchestrator's SIGTERM stop the pod the way Ctrl-C does.

    A container's PID 1 ignores any signal it has no handler for, so without
    this Kubernetes waits out its grace period and kills the pod outright, and
    the run it was serving stays "running" until its activity timeout.
    """
    signal.signal(signal.SIGTERM, _as_ctrl_c)


def _as_ctrl_c(_signum: int, _frame: FrameType | None) -> None:
    signal.raise_signal(signal.SIGINT)


__all__ = [
    "API_KEY_ENV",
    "API_SECRET_ENV",
    "ApiManagerSettings",
    "DeploymentIncomplete",
    "OpenTextDeployment",
    "OpenTextPodConfiguration",
    "load_deployment",
    "stop_on_sigterm",
]
