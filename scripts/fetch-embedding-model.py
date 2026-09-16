#!/usr/bin/env python
"""Download the embedding model's weights into the cache, and prove they load.

Used three times: by `Dockerfile.backend` to bake the weights into the image
(ADR-0004 §2, so no document ever waits on a download), by CI to warm its cache
before the `embeddings` tests run, and by a developer whose tests are skipping.

Prints the model's measured dimension and input limit, because a fetch that
succeeded and a model that works are different claims.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from document_processing.embedder import FastEmbedProvider  # noqa: E402


def main() -> int:
    model_id = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    cache_dir = os.environ.get("EMBEDDING_CACHE_DIR") or None
    provider = FastEmbedProvider(model_id, cache_dir=cache_dir)
    print(
        f"{provider.model_id}: {provider.dimension} dimensions, "
        f"{provider.max_input_tokens} input tokens, "
        f"tokenizer {provider.tokenizer.id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
