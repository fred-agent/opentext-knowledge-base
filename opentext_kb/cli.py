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
A developer tool: one synchronization run against a Content Server folder, without
waiting for a deployment to dispatch it.

It builds exactly the configuration a team's form will produce and calls exactly
the code a dispatched run will call. By default the only thing standing in for
Fred is the boundary, which logs what it would write; name a library with
`--library` and the documents really go there — and a second run against that
library is genuinely incremental, because the library itself is what says what
it already holds.

Pointed at the fake Content Server (`make fake-content-server-run`) with `--base-url`, it needs
no network at all.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import ssl
import time
from collections.abc import Sequence
from dataclasses import asdict

from opentext_kb.content_server import ContentServerSource, Gateway
from opentext_kb.deployment import (
    API_KEY_ENV,
    API_SECRET_ENV,
    DeploymentIncomplete,
    OpenTextDeployment,
    load_deployment,
)
from opentext_kb.document_boundary import LoggingBoundary, open_boundary
from opentext_kb.report import RunReport
from opentext_kb.settings import (
    DEFAULT_PROFILE,
    INGESTION_PROFILES,
    ConfigurationError,
    Settings,
    read_settings,
)
from opentext_kb.synchronize import synchronize
from opentext_kb.tls import CA_FILE_ENV, TrustStoreError, configured_ca_file, tls_policy

# Never a command-line flag: an argument is visible to every process on the
# machine for as long as the run lasts. What follows is the variable's name.
PASSWORD_ENV = "OPENTEXT_KB_PASSWORD"  # nosec B105  # pragma: allowlist secret

DEFAULT_INTERVAL_SECONDS = 60


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="opentext-kb-dev",
        description="Try this Knowledge Base without a Fred deployment.",
        epilog=(
            f"The account's password is read from ${PASSWORD_ENV}; with "
            f"--gateway thales-api-manager, the subscription from ${API_KEY_ENV} "
            f"and ${API_SECRET_ENV}. A "
            f"private certificate authority is added through ${CA_FILE_ENV}."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("declaration", help="print what `publish` would send to Fred")

    for name, help_text in (
        ("sync", "synchronize a folder once, locally"),
        ("watch", "synchronize a folder over and over, until stopped"),
    ):
        run = commands.add_parser(name, help=help_text)
        run.add_argument("--folder-id", required=True, help="the folder's node id")
        run.add_argument("--account", required=True, help="the Content Server account")
        run.add_argument(
            "--base-url",
            default="",
            help="Content Server's REST API root (default: the pod configuration's)",
        )
        run.add_argument(
            "--gateway",
            choices=("none", "thales-api-manager"),
            default="",
            help="what stands in front of Content Server (default: the pod configuration's)",
        )
        run.add_argument(
            "--api-username",
            default="",
            help="the API Manager user (default: the pod configuration's)",
        )
        run.add_argument("--include", default="", help="default: common document types")
        run.add_argument("--exclude", default="")
        run.add_argument("--max-files", type=int, default=None)
        run.add_argument(
            "--profile",
            choices=INGESTION_PROFILES,
            default=DEFAULT_PROFILE,
            help="ingestion profile (default: medium)",
        )
        run.add_argument(
            "--trust-any-certificate",
            action="store_true",
            help=f"accept any certificate; prefer ${CA_FILE_ENV}",
        )
        run.add_argument(
            "--library",
            default="",
            help=(
                "write into this Fred library for real, instead of logging what "
                "would be written. Needs the pod configuration (config/.env)."
            ),
        )
        if name == "watch":
            run.add_argument(
                "--interval",
                type=int,
                default=DEFAULT_INTERVAL_SECONDS,
                help=f"seconds between passes (default: {DEFAULT_INTERVAL_SECONDS})",
            )
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if arguments.command == "declaration":
        # The same projection the image publishes.
        from fred_sdk.knowledge_base import KnowledgeBaseDeclaration

        from opentext_kb.knowledge_base import kb

        print(json.dumps(KnowledgeBaseDeclaration.of(kb).to_payload(), indent=2))
        return 0

    configuration: dict[str, object] = {
        "folder_id": arguments.folder_id,
        "account": arguments.account,
        "password": os.getenv(PASSWORD_ENV, ""),
        "include": arguments.include,
        "exclude": arguments.exclude,
        "profile": arguments.profile,
        "trust_any_certificate": bool(arguments.trust_any_certificate),
    }
    if arguments.max_files is not None:
        configuration["max_files"] = arguments.max_files

    pod = load_deployment()
    deployment = pod.opentext if pod is not None else OpenTextDeployment()
    overrides: dict[str, object] = {}
    if arguments.base_url:
        overrides["base_url"] = arguments.base_url
    if arguments.gateway:
        overrides["gateway"] = arguments.gateway
    if arguments.api_username:
        overrides["api_manager"] = deployment.api_manager.model_copy(
            update={"username": arguments.api_username}
        )
    deployment = deployment.model_copy(update=overrides)

    try:
        settings = read_settings(configuration)
        verify = tls_policy(
            trust_any_certificate=settings.trust_any_certificate,
            ca_file=configured_ca_file(),
        )
        deployment.address()
        gateway = deployment.gateway_in_front()
    except ConfigurationError as error:
        print(f"{error.code}: {error}")
        return 2
    except TrustStoreError as error:
        print(f"ca_file_unreadable: {error}")
        return 2
    except DeploymentIncomplete as error:
        print(f"deployment_incomplete: {error}")
        return 2

    run = _Run(settings, deployment, gateway, verify, arguments.library)
    if arguments.command == "watch":
        return _watch(run, arguments.interval)

    report = asyncio.run(run())
    print(json.dumps(as_json(report), indent=2))
    return 0 if report.succeeded else 1


class _Run:
    """One synchronization, exactly as a dispatched run performs it."""

    def __init__(
        self,
        settings: Settings,
        deployment: OpenTextDeployment,
        gateway: Gateway | None,
        verify: ssl.SSLContext | bool,
        library_id: str,
    ) -> None:
        self.settings = settings
        self.deployment = deployment
        self.gateway = gateway
        self.verify = verify
        self.library_id = library_id

    async def __call__(self) -> RunReport:
        source = ContentServerSource(
            folder_id=self.settings.folder_id,
            account=self.settings.account,
            gateway=self.gateway,
            max_file_bytes=self.deployment.max_file_bytes,
            base_url=self.deployment.address(),
            verify=self.verify,
            page_size=self.deployment.page_size,
            listing_concurrency=self.deployment.listing_concurrency,
        )
        # Named a library, and the pod environment is there: the documents really
        # go to Fred, through the same boundary a dispatched run uses. Without one
        # this stays a dry run, which is what makes the tool useful with no Fred.
        boundary = (
            open_boundary(
                load_deployment(),
                library_id=self.library_id,
                profile=self.settings.profile,
            )
            if self.library_id
            else LoggingBoundary(profile=self.settings.profile)
        )
        try:
            return await synchronize(
                settings=self.settings,
                source=source,
                boundary=boundary,
                concurrency=self.deployment.concurrency,
                max_file_bytes=self.deployment.max_file_bytes,
            )
        finally:
            await boundary.aclose()
            await source.aclose()


def _watch(run: _Run, interval: int) -> int:
    """What Fred's schedule will do, at a pace a developer can watch."""
    watcher = logging.getLogger("watch")
    watcher.info(
        "watching folder %s every %ss — Ctrl-C to stop",
        run.settings.folder_id,
        interval,
    )
    while True:
        report = asyncio.run(run())
        watcher.info("%s", report.summary())
        for issue in report.errors:
            watcher.warning("  %s %s", issue.code, issue.subject or "")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            watcher.info("stopped")
            return 0


def as_json(report: RunReport) -> dict[str, object]:
    """The run, in the shape a person reading a terminal wants it."""
    return {
        "summary": report.summary(),
        "succeeded": report.succeeded,
        "exhaustive": report.exhaustive,
        "discovered": report.discovered,
        "created": report.created,
        "updated": report.updated,
        "removed": report.removed,
        "unchanged": report.unchanged,
        "skipped": report.skipped,
        "pending": report.pending,
        "warnings": [asdict(issue) for issue in report.warnings],
        "errors": [asdict(issue) for issue in report.errors],
        "metrics": report.metrics(),
    }
