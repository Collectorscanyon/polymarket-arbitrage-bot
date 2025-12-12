"""Shared HTTP client utilities.

Goal: centralize request defaults (timeouts, retries, connection pooling)
so network-heavy loops don't silently degrade or DDOS endpoints.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    # requests vendors urllib3, but it's also an explicit dependency.
    from urllib3.util.retry import Retry  # type: ignore
except Exception:  # pragma: no cover
    Retry = None  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (3.05, 10)  # (connect, read)


def _build_retry() -> Optional[Any]:
    if Retry is None:
        return None

    # Conservative retries: handle transient network issues + 429/5xx.
    return Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"),
        raise_on_status=False,
        respect_retry_after_header=True,
    )


def build_session() -> requests.Session:
    s = requests.Session()

    retry = _build_retry()
    if retry is not None:
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)

    return s


session = build_session()


def request(method: str, url: str, *, timeout: Any = None, **kwargs) -> requests.Response:
    """Perform an HTTP request with shared defaults."""
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    return session.request(method=method, url=url, timeout=timeout, **kwargs)


def get_json(url: str, *, timeout: Any = None, **kwargs) -> Any:
    """GET and parse JSON; raises for non-2xx."""
    resp = request("GET", url, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def post_json(url: str, data: Any = None, *, timeout: Any = None, **kwargs) -> Any:
    """POST JSON data and parse response; raises for non-2xx."""
    resp = request("POST", url, timeout=timeout, json=data, **kwargs)
    resp.raise_for_status()
    return resp.json()


def delete(url: str, *, timeout: Any = None, **kwargs) -> requests.Response:
    """DELETE request; returns response."""
    return request("DELETE", url, timeout=timeout, **kwargs)
