"""Proxy architecture: CC points at our TLS endpoint via ANTHROPIC_BASE_URL,
we trim/retrieve-inject every outgoing /v1/messages request, then forward to
Anthropic. Replaces the hook-based plugin path.
"""
