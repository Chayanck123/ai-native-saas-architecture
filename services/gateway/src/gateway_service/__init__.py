"""Gateway service: the single public, API-first entry point.

Authenticates users (JWT) and integrations (scoped API keys), manages
workspaces, members and roles, enforces per-plan rate limits and idempotency
keys, proxies to internal services with short-lived internal tokens, and
delivers signed outbound webhooks.
"""
