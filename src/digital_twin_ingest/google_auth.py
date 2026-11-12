"""Google OAuth 2.0 installed-app flow with a locally cached refresh token."""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Full Drive scope: required to write into pre-existing folders and move processed docs.
SCOPES = ["https://www.googleapis.com/auth/drive"]


class AuthError(RuntimeError):
    pass


def _save_token(token_file: Path, creds: Credentials) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    if os.name == "posix":
        os.chmod(token_file, 0o600)


def get_credentials(
    client_secret_file: Path, token_file: Path, *, interactive: bool = True
) -> Credentials:
    """Return valid credentials, refreshing or (if interactive) re-consenting as needed."""
    creds: Credentials | None = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
        if not creds.has_scopes(SCOPES):
            creds = None  # scope changed since consent — re-authorize

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None  # revoked/expired refresh token
        else:
            _save_token(token_file, creds)
            return creds

    if not interactive:
        raise AuthError("No valid Google token. Run: python -m digital_twin_ingest auth")
    if not client_secret_file.exists():
        raise AuthError(
            f"OAuth client secret not found at {client_secret_file}. "
            "Download a Desktop OAuth client JSON from Google Cloud Console."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token(token_file, creds)
    return creds
