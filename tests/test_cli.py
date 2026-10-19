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
"""The developer tool, which is how anyone first meets this Knowledge Base."""

from __future__ import annotations

import json

import pytest

from opentext_kb.cli import PASSWORD_ENV, main
from opentext_kb.deployment import API_KEY_ENV, API_SECRET_ENV


def test_declaration_prints_what_publish_would_send(capsys: pytest.CaptureFixture[str]):
    assert main(["declaration"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "fred.opentext.folder"


def test_a_folder_id_that_cannot_be_one_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    assert main(["sync", "--folder-id", "not-a-number", "--account", "a"]) == 2
    assert "folder_id_invalid" in capsys.readouterr().out


def test_the_password_is_read_from_the_environment_never_from_the_command_line(
    capsys: pytest.CaptureFixture[str],
):
    """An argument is visible to every process on the machine while it runs."""
    # No password in the environment, so the run is refused before anything is
    # opened — which is enough to prove where the password was looked for.
    assert main(["sync", "--folder-id", "123", "--account", "a"]) == 2
    assert "account_missing" in capsys.readouterr().out


def test_a_deployment_without_an_address_is_named_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv(PASSWORD_ENV, "pw")

    assert main(["sync", "--folder-id", "123", "--account", "a"]) == 2
    assert "deployment_incomplete" in capsys.readouterr().out


def test_a_missing_subscription_is_named_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv(PASSWORD_ENV, "pw")
    monkeypatch.setenv(API_KEY_ENV, "key")
    monkeypatch.setenv(API_SECRET_ENV, "secret")

    # The key and secret are there; the API Manager user is not.
    arguments = ["sync", "--folder-id", "123", "--account", "a"]
    arguments += [
        "--base-url",
        "https://cs.example.com/api/v2",
        "--gateway",
        "thales-api-manager",
    ]
    assert main(arguments) == 2
    assert "deployment_incomplete" in capsys.readouterr().out


def test_an_invalid_profile_is_rejected_by_the_cli():
    with pytest.raises(SystemExit) as error:
        main(["sync", "--folder-id", "123", "--account", "a", "--profile", "turbo"])
    assert error.value.code == 2
