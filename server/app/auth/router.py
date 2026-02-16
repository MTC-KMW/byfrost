"""Auth router - GitHub OAuth and JWT token management.

Endpoints (implemented in Task 1.3):
    GET  /auth/github          - Redirect to GitHub OAuth
    GET  /auth/github/callback - Handle callback, issue JWT
    POST /auth/refresh         - Refresh expiring JWT
"""

from fastapi import APIRouter

router = APIRouter()
