"""NexusMesh Guard shared MongoDB asset/document store.

All binary artifacts (claim images, policy PDFs, benchmark images) live in GridFS;
reference data (fraud DB, ring graph, investigation history, NAIC cache, OFAC SDN)
live in regular collections.
"""

from storage.mongo_assets import (
    ASSET_BUCKET,
    KIND_CLAIM_IMAGE,
    KIND_POLICY_PDF,
    KIND_BENCHMARK_IMAGE,
    KIND_DOCUMENT,
    COLL_FRAUD_DB,
    COLL_RING,
    COLL_INVESTIGATION,
    COLL_NAIC,
    COLL_OFAC,
    COLL_CLAIM_CASES,
    put_asset,
    get_asset_bytes,
    resolve_to_tempfile,
    find_assets,
    find_one_asset,
    upsert_documents,
    load_documents,
    suffix_for,
)

__all__ = [
    "ASSET_BUCKET",
    "KIND_CLAIM_IMAGE",
    "KIND_POLICY_PDF",
    "KIND_BENCHMARK_IMAGE",
    "KIND_DOCUMENT",
    "COLL_FRAUD_DB",
    "COLL_RING",
    "COLL_INVESTIGATION",
    "COLL_NAIC",
    "COLL_OFAC",
    "COLL_CLAIM_CASES",
    "put_asset",
    "get_asset_bytes",
    "resolve_to_tempfile",
    "find_assets",
    "find_one_asset",
    "upsert_documents",
    "load_documents",
    "suffix_for",
]
