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
The whole authoring surface: what a team fills in, and one handler.

Everything this file does is declare and delegate. The declaration is what
`publish` sends to Fred, so it is what a team's form is drawn from; the handler
is what a dispatched run calls. The synchronization itself lives in
`synchronize.py` and knows nothing about Fred.
"""

from __future__ import annotations

from fred_sdk.knowledge_base import (
    FieldSpec,
    KnowledgeBase,
    KnowledgeBaseIssue,
    KnowledgeBaseReconciliation,
    KnowledgeBaseRunContext,
    KnowledgeBaseRunOutcome,
    KnowledgeBaseSyncResult,
    UIHints,
)

from opentext_kb.content_server import ContentServerSource
from opentext_kb.deployment import (
    DeploymentIncomplete,
    OpenTextDeployment,
    load_deployment,
)
from opentext_kb.document_boundary import open_boundary
from opentext_kb.report import Issue, RunReport
from opentext_kb.settings import (
    DEFAULT_INCLUDE,
    DEFAULT_MAX_FILES,
    DEFAULT_PROFILE,
    INGESTION_PROFILES,
    ConfigurationError,
    read_settings,
)
from opentext_kb.synchronize import synchronize as reconcile
from opentext_kb.tls import TrustStoreError, configured_ca_file, tls_policy

kb = KnowledgeBase(
    id="fred.opentext.folder",
    version="1.0.0",
    name="OpenText Content Server folder",
    description=(
        "Synchronize the documents of an OpenText Content Server folder and its "
        "subfolders."
    ),
    configuration_fields=[
        FieldSpec(
            key="folder_id",
            type="string",
            title="Folder",
            description=(
                "The number Content Server shows for the folder: the last part "
                "of its address, as in …/nodes/123456789."
            ),
            required=True,
            ui=UIHints(placeholder="123456789"),
        ),
        FieldSpec(
            key="account",
            type="string",
            title="Account",
            description=(
                "The Content Server account the folder is read with. Only what "
                "it may open reaches the library."
            ),
            required=True,
        ),
        FieldSpec(
            key="password",
            type="secret",
            title="Password",
            description="The account's password.",
            required=True,
        ),
        FieldSpec(
            key="include",
            type="string",
            title="Files",
            description=(
                "Which files to take, written the way .gitignore is, upper or "
                "lower case alike. Separate several with commas."
            ),
            default=DEFAULT_INCLUDE,
        ),
        FieldSpec(
            key="exclude",
            type="string",
            title="Except",
            description="Which of those to leave out. Same way of writing them.",
        ),
        FieldSpec(
            key="profile",
            type="select",
            title="Ingestion profile",
            description="Processing profile sent to Fred for each document written.",
            default=DEFAULT_PROFILE,
            enum=list(INGESTION_PROFILES),
        ),
        FieldSpec(
            key="max_files",
            type="integer",
            title="Maximum documents",
            description=(
                "A run carries at most this many and leaves the rest until the "
                "bound is raised. A bounded run never removes anything."
            ),
            default=DEFAULT_MAX_FILES,
            min=1,
        ),
        FieldSpec(
            key="trust_any_certificate",
            type="boolean",
            title="Accept any certificate (not protected against interception)",
            description=(
                "Only when this deployment cannot be given Content Server's certificate "
                "authority. Prefer trusting the authority itself through "
                "$OPENTEXT_KB_CA_FILE. Every run using this reports a warning."
            ),
            default=False,
        ),
    ],
)


@kb.synchronize
async def synchronize(context: KnowledgeBaseRunContext) -> KnowledgeBaseSyncResult:
    """Bring this run's library to what its Content Server folder now holds."""
    pod = load_deployment()
    deployment = pod.opentext if pod is not None else OpenTextDeployment()
    try:
        settings = read_settings(context.configuration)
        verify = tls_policy(
            trust_any_certificate=settings.trust_any_certificate,
            ca_file=configured_ca_file(),
        )
        base_url = deployment.address()
        gateway = deployment.gateway_in_front()
    except ConfigurationError as error:
        return _refused(error.code, str(error))
    except TrustStoreError as error:
        # A deployment fault, not a team's: worth its own code so it is not
        # read as "Content Server is down".
        return _refused("ca_file_unreadable", str(error))
    except DeploymentIncomplete as error:
        return _refused("deployment_incomplete", str(error))

    source = ContentServerSource(
        folder_id=settings.folder_id,
        account=settings.account,
        gateway=gateway,
        max_file_bytes=deployment.max_file_bytes,
        base_url=base_url,
        verify=verify,
        page_size=deployment.page_size,
        listing_concurrency=deployment.listing_concurrency,
    )
    boundary = open_boundary(
        pod, library_id=context.library_id, profile=settings.profile
    )
    try:
        report = await reconcile(
            settings=settings,
            source=source,
            boundary=boundary,
            concurrency=deployment.concurrency,
            max_file_bytes=deployment.max_file_bytes,
        )
    finally:
        await boundary.aclose()
        await source.aclose()

    # `reconcile` has already logged the summary: one line per run.
    return _result(report)


def _result(report: RunReport) -> KnowledgeBaseSyncResult:
    """Say what happened in Fred's vocabulary.

    `reconciliation` is the field that matters: it is what tells Fred this run
    saw the whole folder, and it is false for every pass that was bounded or
    cut short.
    """
    return KnowledgeBaseSyncResult(
        outcome=(
            KnowledgeBaseRunOutcome.succeeded
            if report.succeeded
            else KnowledgeBaseRunOutcome.failed
        ),
        reconciliation=(
            KnowledgeBaseReconciliation.complete
            if report.exhaustive
            else KnowledgeBaseReconciliation.partial
        ),
        summary=report.summary(),
        discovered=report.discovered,
        created=report.created,
        updated=report.updated,
        removed=report.removed,
        unchanged=report.unchanged,
        warnings=[_issue(issue) for issue in report.warnings],
        errors=[_issue(issue) for issue in report.errors],
        metrics=report.metrics(),
    )


def _issue(issue: Issue) -> KnowledgeBaseIssue:
    return KnowledgeBaseIssue(
        code=issue.code, message=issue.message, subject=issue.subject
    )


def _refused(code: str, message: str) -> KnowledgeBaseSyncResult:
    """A run that never read Content Server proves nothing about the library."""
    return KnowledgeBaseSyncResult(
        outcome=KnowledgeBaseRunOutcome.failed,
        reconciliation=KnowledgeBaseReconciliation.partial,
        summary=message,
        errors=[KnowledgeBaseIssue(code=code, message=message)],
    )
