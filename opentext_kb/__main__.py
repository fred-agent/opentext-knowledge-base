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
The image's entry point — the whole integration with Fred.

    python -m opentext_kb publish   # declare this KB, then exit
    python -m opentext_kb run       # serve runs until stopped

The SDK owns both commands. What this adds is the deployment's own part: the
`opentext:` settings are checked before anything is served, and SIGTERM stops the
pod cleanly (see `deployment.py`).
"""

from fred_sdk.knowledge_base import knowledge_base_main

from opentext_kb.deployment import load_deployment, stop_on_sigterm
from opentext_kb.knowledge_base import kb

stop_on_sigterm()
load_deployment()
try:
    raise SystemExit(knowledge_base_main(kb))
except KeyboardInterrupt:
    # Stopped by SIGTERM or Ctrl-C, after the worker has handed back the run it
    # was serving. The conventional status of a process ended by SIGINT.
    raise SystemExit(130) from None
