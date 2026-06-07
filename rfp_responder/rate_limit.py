"""
rate_limit.py – Process-wide slowapi Limiter singleton.

Defined here (not in main.py) so both main.py and routes.py can import it
without creating a circular dependency.

Rate limit strategy
───────────────────
Key function: X-Tenant-ID header → per-tenant limits so one noisy tenant
cannot starve others.  Falls back to client IP for unauthenticated requests.

The Limiter is attached to app.state in main.py so SlowAPIMiddleware can
access it.  Route decorators (`@limiter.limit(...)`) read the same instance.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _tenant_or_ip(request: Request) -> str:
    """Rate-limit key: prefer tenant identity, fall back to IP address."""
    return request.headers.get("X-Tenant-ID") or get_remote_address(request)


limiter = Limiter(key_func=_tenant_or_ip, default_limits=[])
