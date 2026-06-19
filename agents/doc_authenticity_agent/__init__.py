"""NexusMesh Guard — Document Authenticity package.

Multi-layer image forensics (EXIF + C2PA + JPEG/ELA + vision) for the Band agent.
"""

from agents.doc_authenticity_agent.tools import (
    DOC_AUTH_TOOLS,
    analyze_document,
    analyze_document_from_mongo,
    analyze_claim_document_tool,
    list_batch_image_claims_tool,
    analyze_batch_documents_tool,
    combine_authenticity,
    run_exif_layer,
    run_c2pa_layer,
    run_compression_layer,
    run_vision_layer,
)
from agents.doc_authenticity_agent.prompt import DOC_AUTH_SYSTEM_PROMPT

__all__ = [
    "DOC_AUTH_TOOLS",
    "DOC_AUTH_SYSTEM_PROMPT",
    "analyze_document",
    "analyze_document_from_mongo",
    "analyze_claim_document_tool",
    "list_batch_image_claims_tool",
    "analyze_batch_documents_tool",
    "combine_authenticity",
    "run_exif_layer",
    "run_c2pa_layer",
    "run_compression_layer",
    "run_vision_layer",
]
