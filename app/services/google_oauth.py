"""Google OAuth2 credentials из refresh token (без token.json)."""

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from app.config import settings

_SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
]

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def get_credentials() -> Credentials:
    """Вернуть свежие credentials, обновив access token через refresh token."""
    creds = Credentials(
        token=None,
        refresh_token=settings.google_refresh_token,
        token_uri=_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=_SCOPES,
    )
    creds.refresh(Request())
    return creds
