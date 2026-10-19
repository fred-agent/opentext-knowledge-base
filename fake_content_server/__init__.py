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
A stand-in for OpenText Content Server: a local folder served through the two
calls the Knowledge Base makes.

By default it stands behind the API Manager gateway, so the signature is
exercised; without a subscription it stands for a Content Server reached
directly. Its signature check is written here from the gateway's reference
formula, not imported from the client, so a mistake in the client fails
against it instead of being reproduced on both sides. A test fixture and a k3d
stand-in; never deployed next to a real Content Server.
"""

from fake_content_server.server import Credentials, FakeContentServer

__all__ = ["Credentials", "FakeContentServer"]
