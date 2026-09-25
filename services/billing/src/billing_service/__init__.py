"""Billing service: usage-based monetisation.

Meters usage idempotently, converts it to credits, enforces plan entitlements,
prices invoices and reports recurring revenue (MRR/ARR, NRR, gross margin).
"""
