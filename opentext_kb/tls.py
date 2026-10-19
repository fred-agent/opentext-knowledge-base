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
How this pod treats a store's certificate, and how it explains a refusal.

A Content Server often sits behind a corporate certificate authority. The
answer is to give the pod that authority, never to stop checking: see
`tls_policy`.
"""

from __future__ import annotations

import os
import ssl

import httpx

from opentext_kb.source import CertificateNotTrusted

# Where a deployment puts a private certificate authority's PEM. Not a
# configuration field: it names a file inside the pod, which nobody filling in
# a form can create.
CA_FILE_ENV = "OPENTEXT_KB_CA_FILE"


class TrustStoreError(ValueError):
    """The certificate authority this pod was told to trust cannot be read.

    Its own error because in a cluster it means one specific thing — a volume
    that did not mount — and because the alternative is a handshake failure
    that names the store rather than the mount.
    """


def configured_ca_file() -> str:
    """The private certificate authority this pod was given, if it was given one."""
    return os.environ.get(CA_FILE_ENV, "").strip()


def tls_policy(
    *, trust_any_certificate: bool, ca_file: str = ""
) -> ssl.SSLContext | bool:
    """How this run treats the store's certificate.

    A private authority is **added** to what is already trusted, never
    substituted for it. $SSL_CERT_FILE replaces the whole trust store: point it
    at one corporate root and this pod stops trusting the authorities its own
    Keycloak, Control Plane and Knowledge Flow may be presenting.
    """
    if trust_any_certificate:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    if not ca_file:
        return True
    context = httpx.create_ssl_context()
    try:
        context.load_verify_locations(cafile=ca_file)
    except ssl.SSLError as error:
        # Before OSError, which it inherits from: the file is there and is not
        # a certificate, which is a different mistake from it being absent.
        raise TrustStoreError(
            f"{ca_file} is not a PEM certificate authority: {error}"
        ) from error
    except OSError as error:
        raise TrustStoreError(
            f"the certificate authority at {ca_file} could not be read: {error}. "
            f"${CA_FILE_ENV} names it; check the volume is mounted there."
        ) from error
    return context


def certificate_rejected(error: BaseException) -> ssl.SSLCertVerificationError | None:
    """The certificate failure inside a transport error, if that is what it is.

    Walked rather than matched on the message: httpx reports a handshake as a
    plain `ConnectError`, and the original is reached through `__context__`.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return current
        current = current.__cause__ or current.__context__
    return None


def not_trusted(
    failure: ssl.SSLCertVerificationError, what: str
) -> CertificateNotTrusted:
    """Say which trust store refused, and what would fix it."""
    ca_file = configured_ca_file()
    remedy = (
        f"${CA_FILE_ENV} names {ca_file}, and that authority does not certify "
        "this store"
        if ca_file
        else "This pod trusts public authorities only; give the issuing "
        f"authority's public root to the pod through ${CA_FILE_ENV}"
    )
    reason = getattr(failure, "verify_message", None) or failure
    return CertificateNotTrusted(
        f"{what}: the store's certificate is signed by an authority this pod "
        f"does not trust ({reason}). {remedy}. Python reads no system trust "
        "store, so curl working on a laptop says nothing about this."
    )
