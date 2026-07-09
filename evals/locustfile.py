"""Load test for the pharmacy agent API.

Two user types:
  * ChatUser  — full path: create a session, then POST /chat with realistic
    pharmacy questions. Exercises the LLM + cache + rate limit. Repeated
    questions hit the Redis cache (no LLM call), which is the realistic steady
    state. Requires a real OPENROUTER_API_KEY on the server.
  * ReadUser  — infra-only: /session/create + /ready. No LLM cost; use this to
    load-test the stack (pool, redis, web layer) without spending tokens.

Run (against the docker stack on :8088):
    locust -f evals/locustfile.py --host http://localhost:8088
    # then open http://localhost:8089 and set users / spawn rate
    # or headless:
    locust -f evals/locustfile.py --host http://localhost:8088 \
           --users 100 --spawn-rate 10 --run-time 2m --headless
"""

import random

from locust import HttpUser, between, task

QUESTIONS = [
    "What is the stock of article 1000000015837 at site 20052-CCTLKK?",
    "Total stock of article 1000000015837 across all sites?",
    "Top 5 articles by stock at site 20052-CCTLKK?",
    "What is the brand name of article 1000000015837?",
    "article 1000000015837 ရဲ့ stock ဘယ်လောက်ရှိလဲ",
    "List substitutes for article 1000000416274",
]


import uuid


def _make_session(client, embed_id=None) -> str:
    # Unique embed_id per virtual user → distinct rate-limit bucket, so the load
    # test measures real concurrency instead of one shared quota.
    embed_id = embed_id or f"load-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/api/embed/session/create",
        json={"embed_id": embed_id, "public_key": "load-test"},
        name="/session/create",
    )
    return r.json().get("session_token", "")


class ChatUser(HttpUser):
    """Realistic user: session then chat (LLM + cache)."""

    weight = 3
    wait_time = between(1, 4)

    def on_start(self):
        self.token = _make_session(self.client)

    @task
    def chat(self):
        self.client.post(
            "/api/embed/chat",
            json={"session_token": self.token, "message": random.choice(QUESTIONS)},
            name="/chat",
        )


class ReadUser(HttpUser):
    """Infra-only load: no LLM cost."""

    weight = 1
    wait_time = between(1, 3)

    @task(3)
    def ready(self):
        self.client.get("/ready", name="/ready")

    @task(1)
    def session(self):
        _make_session(self.client)
