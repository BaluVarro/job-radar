"""Polite HTTP session: identifies itself, retries briefly, waits between requests."""

from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = ("Mozilla/5.0 (compatible; job-radar/0.1; "
              "+https://github.com/BaluVarro/job-radar)")
TIMEOUT = 30
DELAY_SECONDS = 2.0   # pause between requests so no site is hammered

_last_request = 0.0


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept-Language": "en,hu;q=0.8"})
    retry = Retry(total=2, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "POST"))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def polite_wait() -> None:
    global _last_request
    elapsed = time.monotonic() - _last_request
    if elapsed < DELAY_SECONDS:
        time.sleep(DELAY_SECONDS - elapsed)
    _last_request = time.monotonic()


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    polite_wait()
    resp = session.get(url, timeout=TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp


def post(session: requests.Session, url: str, **kwargs) -> requests.Response:
    polite_wait()
    resp = session.post(url, timeout=TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp
