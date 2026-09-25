"""Knowledge service: retrieval-augmented generation (RAG) building blocks.

Documents are chunked, embedded into vectors and stored per tenant; search fuses
semantic (vector) and keyword (BM25 / full-text) rankings, boosted by user
feedback.
"""
