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
What the operator decides, and how the pod stops.

The settings are read from the same file, by the same loader, as the rest of
the pod's configuration — so these tests go through that file rather than
around it. The stop is tested for the one property that matters: SIGTERM ends
a running pod by cancelling it, which is what lets the workflow engine hand the
interrupted run to another pod rather than wait hours for it.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from opentext_kb.deployment import (
    API_KEY_ENV,
    API_SECRET_ENV,
    ApiManagerSettings,
    DeploymentIncomplete,
    OpenTextDeployment,
    OpenTextPodConfiguration,
    load_deployment,
    stop_on_sigterm,
)

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "configuration.yaml"


def test_the_shipped_configuration_states_the_defaults_it_documents():
    """The file an operator copies must parse, and say what the code does."""
    pod = OpenTextPodConfiguration.model_validate(_yaml(SHIPPED.read_text()))

    assert pod.opentext == OpenTextDeployment()


def test_a_configuration_without_the_section_takes_the_defaults():
    body = _without_section(SHIPPED.read_text())
    pod = OpenTextPodConfiguration.model_validate(_yaml(body))

    assert pod.opentext == OpenTextDeployment()


def test_the_section_is_read_through_the_pod_s_own_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = tmp_path / "configuration.yaml"
    config.write_text(
        _without_section(SHIPPED.read_text())
        + "\nopentext:\n  concurrency: 12\n  ingestion_wait_seconds: 1800\n"
    )
    monkeypatch.setenv("CONFIG_FILE", str(config))

    pod = load_deployment()

    assert pod is not None
    assert pod.opentext.concurrency == 12
    assert pod.opentext.ingestion_wait_seconds == 1800


def test_no_configuration_at_all_is_a_developer_tool_not_an_error():
    # conftest points $CONFIG_FILE at a path that does not exist.
    assert load_deployment() is None


@pytest.mark.parametrize(
    "section",
    [
        {"concurency": 12},  # misspelt: refused rather than silently ignored
        {"concurrency": 0},
        {"concurrency": 65},
        {"ingestion_wait_seconds": 0},
        {"concurrency": "many"},
        {"api_manager": {"hash_algorithm": "SHA-1"}},
        {"gateway": "nginx"},
        {"page_size": 0},
        {"max_file_mib": 0},
    ],
)
def test_a_value_the_operator_got_wrong_is_refused(section: dict[str, object]):
    with pytest.raises(ValidationError):
        OpenTextDeployment.model_validate(section)


def test_without_an_address_a_deployment_is_incomplete():
    with pytest.raises(DeploymentIncomplete, match="base_url"):
        OpenTextDeployment().address()


def test_no_gateway_is_the_default_and_needs_no_secret():
    assert (
        OpenTextDeployment(base_url="https://cs.example.com/api/v2").gateway_in_front()
        is None
    )


def test_the_api_manager_is_built_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(API_KEY_ENV, "key")
    monkeypatch.setenv(API_SECRET_ENV, "secret")
    deployment = OpenTextDeployment(
        gateway="thales-api-manager",
        api_manager=ApiManagerSettings(username="apim-user"),
    )

    gateway = deployment.gateway_in_front()

    assert gateway is not None
    headers = gateway.headers("GET", "https://x/v2/nodes/1/nodes?limit=1&page=1")
    assert (headers["apikey"], headers["username"]) == ("key", "apim-user")


@pytest.mark.parametrize("missing", [API_KEY_ENV, API_SECRET_ENV, "username"])
def test_half_a_subscription_is_refused_with_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
):
    for name in (API_KEY_ENV, API_SECRET_ENV):
        if name != missing:
            monkeypatch.setenv(name, "value")
    deployment = OpenTextDeployment(
        gateway="thales-api-manager",
        api_manager=ApiManagerSettings(
            username="" if missing == "username" else "apim-user"
        ),
    )

    with pytest.raises(DeploymentIncomplete, match="OPENTEXT_KB_API_KEY"):
        deployment.gateway_in_front()


def test_sigterm_stops_a_running_pod_the_way_ctrl_c_does():
    """Cancelled, not killed: the worker gets to hand its run back."""
    previous = signal.getsignal(signal.SIGTERM)
    cancelled: list[bool] = []

    async def serve() -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    stop_on_sigterm()
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(serve())
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert cancelled == [True]


def _yaml(text: str) -> dict[str, object]:
    return yaml.safe_load(text)


def _without_section(text: str) -> str:
    return text.split("\nopentext:", 1)[0]
