"""Vyasa POC - client for the api's learner-record endpoints (used by agent.py).

Talks to api/main.py's GET/PUT /records/{learner_id}, authenticated with the
shared X-Drona-Secret header. Kept deliberately small: two functions, no retries,
because a POC agent should fail loudly rather than silently lose a record.

Performance note: creating an httpx client loads the whole certificate bundle,
which took 1-3.6 s per call on the dev machine and froze the agent's audio loop.
So one client is created per event loop and reused, it is built in a worker
thread so the loop never stalls, and certificates are skipped entirely when the
api is plain http (the local default).
"""
import asyncio
import os

import httpx

_clients: dict[int, httpx.AsyncClient] = {}   # one client per event loop


def _base_url() -> str:
    return os.getenv("DRONA_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _secret() -> str:
    return os.getenv("DRONA_API_SECRET", "")


def _headers() -> dict:
    return {"X-Drona-Secret": _secret()}


def _build_client() -> httpx.AsyncClient:
    # Plain http never uses certificates, so don't load them (that is the slow part).
    # An https api (for example once this is deployed) still verifies normally.
    plain_http = _base_url().startswith("http://")
    return httpx.AsyncClient(timeout=10, verify=not plain_http)


async def _client() -> httpx.AsyncClient:
    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None or client.is_closed:
        # No lock on purpose: a lock made at import time can be tied to the wrong loop.
        # Worst case two calls race and one spare client is built once.
        client = await asyncio.to_thread(_build_client)  # keep the audio loop free
        _clients[loop_id] = client
    return client


def empty_record(learner_id: str) -> dict:
    """Section 6.12 shape, with no lessons yet."""
    return {"learner_id": learner_id, "lessons": []}


async def get(learner_id: str) -> dict:
    """Return the learner's record, or an empty one if the api has none (404)."""
    if not _secret():
        raise RuntimeError("DRONA_API_SECRET is not set in agent/.env")
    url = f"{_base_url()}/records/{learner_id}"
    client = await _client()
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 404:
        return empty_record(learner_id)
    resp.raise_for_status()
    return resp.json()


async def put(learner_id: str, record: dict) -> None:
    """Save the learner's record. Raises on any non-2xx response - a failed
    save should be visible, not swallowed."""
    if not _secret():
        raise RuntimeError("DRONA_API_SECRET is not set in agent/.env")
    url = f"{_base_url()}/records/{learner_id}"
    client = await _client()
    resp = await client.put(url, headers=_headers(), json=record)
    resp.raise_for_status()
