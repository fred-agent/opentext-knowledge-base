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
What the image declares to Fred, and what it reports back.

The declaration is the only thing a team ever sees of this Knowledge Base, and
it is published by a deployment hook nobody watches — so what it contains is
worth pinning down here rather than discovering in a form.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fred_sdk.knowledge_base import (
    KnowledgeBaseDeclaration,
    KnowledgeBaseReconciliation,
    KnowledgeBaseRunContext,
    KnowledgeBaseRunOutcome,
)
from fred_sdk.knowledge_base.schedule import is_platform_field

from opentext_kb.knowledge_base import _result, kb, synchronize
from opentext_kb.report import RunReport
from opentext_kb.settings import read_settings


def test_the_declaration_publishes_as_json():
    payload = KnowledgeBaseDeclaration.of(kb).to_payload()

    assert payload["id"] == "fred.opentext.folder"
    assert json.loads(json.dumps(payload)) == payload


def test_no_declared_field_takes_a_key_fred_declares():
    """The form has two zones sharing one key space; only one of them is ours."""
    assert not any(is_platform_field(field.key) for field in kb.configuration_fields)


def test_every_declared_field_is_one_the_run_actually_reads():
    """A field in the form that nothing reads is a promise to a team we break."""
    filled: dict[str, object] = {field.key: "" for field in kb.configuration_fields}
    filled["folder_id"] = "123456789"
    filled["account"] = "reader"
    filled["password"] = "secret"  # pragma: allowlist secret
    filled["include"] = "**/*.pdf"
    filled["exclude"] = "drafts/**"
    filled["max_files"] = 10
    filled["profile"] = "rich"
    filled["trust_any_certificate"] = True

    settings = read_settings(filled)

    assert settings.folder_id == "123456789"
    assert settings.account.name == "reader"
    assert settings.max_files == 10
    assert settings.profile == "rich"
    assert settings.trust_any_certificate is True
    assert settings.selection.selects("a.pdf")
    assert not settings.selection.selects("drafts/a.pdf")


def test_the_folder_and_the_account_are_what_a_team_must_fill_in():
    required = {field.key for field in kb.configuration_fields if field.required}

    assert required == {"folder_id", "account", "password"}


def test_the_password_is_declared_a_secret():
    field = next(item for item in kb.configuration_fields if item.key == "password")
    assert field.type == "secret"


def test_the_certificate_field_is_off_by_default_in_the_form():
    """A dangerous default is one nobody ever notices they accepted."""
    field = next(
        item for item in kb.configuration_fields if item.key == "trust_any_certificate"
    )
    assert field.default is False


def test_a_run_reports_fred_s_counters_from_what_this_implementation_counted():
    report = RunReport(exhaustive=True, discovered=4, unchanged=1)
    report.wrote(created=True, size_bytes=10)
    report.wrote(created=False, size_bytes=20)
    report.removed = 1
    report.skip("big.md", "too_large")

    result = _result(report)

    assert result.outcome is KnowledgeBaseRunOutcome.succeeded
    assert result.reconciliation is KnowledgeBaseReconciliation.complete
    assert (
        result.discovered,
        result.created,
        result.updated,
        result.removed,
        result.unchanged,
    ) == (4, 1, 1, 1, 1)
    assert [issue.code for issue in result.warnings] == ["too_large"]


def test_a_bounded_run_is_reported_as_incomplete_even_though_it_succeeded():
    """The two are orthogonal, and only one of them licenses a deletion."""
    result = _result(RunReport(exhaustive=False))

    assert result.outcome is KnowledgeBaseRunOutcome.succeeded
    assert result.reconciliation is KnowledgeBaseReconciliation.partial


def test_a_failed_run_is_reported_as_one():
    report = RunReport(exhaustive=True)
    report.fail("write_failed", "refused", subject="a.md")

    result = _result(report)

    assert result.outcome is KnowledgeBaseRunOutcome.failed
    assert [issue.subject for issue in result.errors] == ["a.md"]


def _run(configuration: dict[str, Any]):
    return asyncio.run(
        synchronize(
            KnowledgeBaseRunContext(
                definition_id="fred.opentext.folder",
                instance_id="instance",
                team_id="team",
                run_id="run",
                library_id="library",
                configuration=configuration,
            )
        )
    )


def test_a_configuration_that_cannot_be_read_never_reaches_server():
    """A handler owes Fred a result, not an exception — and no request at all."""
    result = _run({"folder_id": "not-a-number"})

    assert result.outcome is KnowledgeBaseRunOutcome.failed
    assert result.reconciliation is KnowledgeBaseReconciliation.partial
    assert [issue.code for issue in result.errors] == ["folder_id_invalid"]


def test_an_incomplete_deployment_is_refused_before_any_request():
    """The operator's fault, named as such rather than read as Content Server being down."""
    result = _run(
        {"folder_id": "123", "account": "a", "password": "p"}
    )  # pragma: allowlist secret

    assert result.outcome is KnowledgeBaseRunOutcome.failed
    assert [issue.code for issue in result.errors] == ["deployment_incomplete"]


def test_the_profile_field_declares_the_supported_choices_and_default():
    field = next(item for item in kb.configuration_fields if item.key == "profile")
    assert field.type == "select"
    assert field.enum == ["fast", "medium", "rich"]
    assert field.default == "medium"
