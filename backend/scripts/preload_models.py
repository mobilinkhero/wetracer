#!/usr/bin/env python3
"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

# This script used to download the local FastEmbed embedding model so the
# first request did not pay for the download. Embeddings now come from the
# hosted provider API (app.knowledge.embedder) using each organization's
# configured provider key, so there is no local model to preload — this
# script is a no-op kept only because scripts/start.sh still calls it.

import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def main():
    """Nothing to preload: embeddings are hosted, no local model exists."""
    logger.info("Embeddings are hosted (app.knowledge.embedder); nothing to preload.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
