"""Benchmark embedding models on Burmese semantic ranking + latency."""

import math
import time

import httpx

from app.config import get_settings

KEY = get_settings().openrouter_api_key
URL = "https://openrouter.ai/api/v1/embeddings"

QUERY = "ဖျားနာ ကိုယ်ပူ အတွက် ဆေး"  # Burmese: medicine for fever
CANDS = {
    "relevant_fever": "PARACAP Paracetamol - fever, pain relief (ဖျား၊ နာကျင်မှု)",
    "rehydration": "ROYAL-D Electrolyte Powder - rehydration",
    "wheelchair": "MEDICARE WHEEL CHAIR - mobility aid",
    "vitaminC": "Vitamin C supplement - immunity",
}
MODELS = [
    "google/gemini-embedding-2",
    "openai/text-embedding-3-large",
    "openai/text-embedding-3-small",
]


def embed(client, model, text):
    t = time.time()
    r = client.post(URL, headers={"Authorization": f"Bearer {KEY}"},
                    json={"model": model, "input": text})
    r.raise_for_status()
    return r.json()["data"][0]["embedding"], time.time() - t


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb)


with httpx.Client(timeout=60) as client:
    for m in MODELS:
        try:
            qv, qt = embed(client, m, QUERY)
            scored, ts = {}, [qt]
            for k, txt in CANDS.items():
                v, dt = embed(client, m, txt)
                scored[k] = cos(qv, v)
                ts.append(dt)
            rank = sorted(scored.items(), key=lambda x: -x[1])
            margin = rank[0][1] - rank[1][1]
            print(m)
            print(f"  dim={len(qv)}  avg_latency={sum(ts)/len(ts)*1000:.0f}ms")
            print(f"  top={rank[0][0]} ({rank[0][1]:.3f})  margin={margin:.3f}  "
                  f"correct={'YES' if rank[0][0]=='relevant_fever' else 'NO'}")
            print(f"  ranks: {[(k, round(v,3)) for k,v in rank]}")
        except Exception as e:
            print(m, "ERR", str(e)[:150])
        print()
