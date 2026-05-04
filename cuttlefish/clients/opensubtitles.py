"""OpenSubtitles.com REST API client.

Reads creds from $OPENSUBTITLES_API_KEY (required for any access),
$OPENSUBTITLES_USERNAME and $OPENSUBTITLES_PASSWORD (required to *download*
files; search works without). Get a free key at
https://www.opensubtitles.com/en/consumers.

If no API key is set, .configured is False and all methods short-circuit so
the rest of cuttlefish keeps working without subtitles.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import httpx

DEFAULT_BASE_URL = "https://api.opensubtitles.com/api/v1"
DEFAULT_USER_AGENT = "cuttlefish/0.1"


class OpenSubtitles:
    def __init__(
        self,
        api_key: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        client: Optional[httpx.Client] = None,
        timeout: float = 15.0,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("OPENSUBTITLES_API_KEY")
        self.username = username if username is not None else os.environ.get("OPENSUBTITLES_USERNAME")
        self.password = password if password is not None else os.environ.get("OPENSUBTITLES_PASSWORD")
        self.user_agent = user_agent
        self._client = client or httpx.Client(timeout=timeout, base_url=base_url)
        self._token: Optional[str] = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def can_download(self) -> bool:
        return self.configured and bool(self.username) and bool(self.password)

    def _headers(self) -> dict:
        h = {
            "Api-Key": self.api_key or "",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def login(self) -> Optional[str]:
        if not self.can_download:
            return None
        r = self._client.post(
            "/login",
            json={"username": self.username, "password": self.password},
            headers=self._headers(),
        )
        r.raise_for_status()
        self._token = r.json().get("token")
        return self._token

    def search(
        self, query: str, languages: str = "en", year: Optional[int] = None
    ) -> list[dict]:
        if not self.configured or not query:
            return []
        params = {"query": query, "languages": languages}
        if year:
            params["year"] = str(year)
        r = self._client.get("/subtitles", params=params, headers=self._headers())
        r.raise_for_status()
        return r.json().get("data") or []

    def download(self, file_id: int, dest: Path) -> Optional[Path]:
        if not self.can_download:
            return None
        if self._token is None:
            self.login()
        r = self._client.post(
            "/download", json={"file_id": file_id}, headers=self._headers()
        )
        r.raise_for_status()
        link = r.json().get("link")
        if not link:
            return None
        # The download link is an absolute URL outside the API base.
        with httpx.Client(timeout=30.0) as plain:
            r2 = plain.get(link)
            r2.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r2.content)
        return dest

    def close(self) -> None:
        self._client.close()
