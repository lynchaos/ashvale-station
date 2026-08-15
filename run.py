#!/usr/bin/env python3
# Copyright 2026 Kemal Yaylali
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

"""Entry point. `python run.py` and open http://<pi>:8000"""

from __future__ import annotations

import argparse

import uvicorn

from ashvale.config import CONFIG


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=CONFIG.server.host)
    ap.add_argument("--port", type=int, default=CONFIG.server.port)
    ap.add_argument("--no-led", action="store_true")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    if args.no_led:
        CONFIG.server.led_enabled = False

    # One worker, one event loop. The station owns mutable model state, so a
    # second worker would give you two divergent forecasters sharing a socket.
    # timeout_graceful_shutdown bounds the wait for in-flight requests. Without
    # it, the dashboard's server-sent-events connection never completes, so a
    # stop blocks until systemd's 90 s timeout and ends in SIGKILL. Measured:
    # with one stream client open, shutdown went from "never" to under 2 s.
    uvicorn.run("ashvale.api:app", host=args.host, port=args.port,
                reload=args.reload, workers=1, log_level="info",
                limit_concurrency=32, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
