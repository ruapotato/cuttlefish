"""TMDb (The Movie Database) client.

Reads API key from $TMDB_API_KEY at instantiation. If no key is set,
.configured is False and all methods short-circuit so the rest of cuttlefish
keeps working without metadata. Get a free key at
https://www.themoviedb.org/settings/api.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import httpx

DEFAULT_BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_IMAGE_BASE = "https://image.tmdb.org/t/p"


class TMDb:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        image_base: str = DEFAULT_IMAGE_BASE,
        client: Optional[httpx.Client] = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("TMDB_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.image_base = image_base.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def search_movie(self, title: str, year: Optional[int] = None) -> list[dict]:
        if not self.configured or not title:
            return []
        params = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = str(year)
        r = self._client.get(
            f"{self.base_url}/search/movie", params=params, headers=self._headers()
        )
        r.raise_for_status()
        return r.json().get("results") or []

    def search_tv(self, title: str) -> list[dict]:
        if not self.configured or not title:
            return []
        r = self._client.get(
            f"{self.base_url}/search/tv",
            params={"query": title, "include_adult": "false"},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json().get("results") or []

    def poster_url(self, poster_path: Optional[str], size: str = "w500") -> Optional[str]:
        if not poster_path:
            return None
        # poster_path returned by TMDb starts with '/'
        return f"{self.image_base}/{size}{poster_path}"

    def download_poster(
        self, poster_path: Optional[str], dest: Path, size: str = "w500"
    ) -> Optional[Path]:
        url = self.poster_url(poster_path, size)
        if not url:
            return None
        r = self._client.get(url)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest

    def close(self) -> None:
        self._client.close()
