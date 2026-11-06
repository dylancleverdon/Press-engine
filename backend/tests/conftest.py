from __future__ import annotations

import json
import random
from collections.abc import Callable

import httpx
import pytest

from press_engine.config import Config
from press_engine.db import Database, set_database
from press_engine.http import Fetcher
from press_engine.services import Services


class Router:
    """Tiny httpx MockTransport router: first matching (method, url-prefix) wins."""

    def __init__(self):
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response] | httpx.Response]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, prefix: str, response):
        self.routes.insert(0, (method.upper(), prefix, response))
        return self

    def json(self, method: str, prefix: str, data, status: int = 200):
        return self.add(method, prefix, lambda req: httpx.Response(status, json=data))

    def html(self, url: str, body: str, status: int = 200):
        return self.add("GET", url, lambda req: httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        for method, prefix, resp in self.routes:
            if request.method == method and url.startswith(prefix):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404, text=f"no route for {request.method} {url}")

    def requests_to(self, prefix: str) -> list[httpx.Request]:
        return [r for r in self.calls if str(r.url).startswith(prefix)]


class FakeLLM:
    name = "fake"

    def __init__(self, json_fn=None, text_fn=None):
        self.json_fn = json_fn or (lambda system, prompt, schema: {})
        self.text_fn = text_fn or (lambda system, prompt: "Draft reply")
        self.json_calls: list[str] = []
        self.text_calls: list[str] = []

    def json(self, system, prompt, schema, *, max_tokens=1024):
        self.json_calls.append(prompt)
        return self.json_fn(system, prompt, schema)

    def text(self, system, prompt, *, max_tokens=1024):
        self.text_calls.append(prompt)
        return self.text_fn(system, prompt)


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    set_database(database)
    return database


@pytest.fixture
def session(db):
    s = db.SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def router():
    return Router()


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path, scheduler_enabled=False, extract_llm_provider="none", score_llm_provider="none",
                  expo_push_url="https://exp.test/push")


@pytest.fixture
def services(cfg, router):
    http = httpx.Client(transport=httpx.MockTransport(router), follow_redirects=True)
    fetcher = Fetcher(http, respect_robots=False, per_host_delay=0)
    return Services(cfg=cfg, http=http, fetcher=fetcher, rng=random.Random(7))


def dumps(obj) -> str:
    return json.dumps(obj)
