"""Multi-query Burmese embedding showdown: gemini-embedding-2 vs 3-small."""

import math
import time

import httpx

from app.config import get_settings

KEY = get_settings().openrouter_api_key
URL = "https://openrouter.ai/api/v1/embeddings"

# (Burmese query, key of the candidate that SHOULD rank first)
TESTS = [
    ("ဖျားနာ အတွက် ဆေး", "fever"),            # fever medicine
    ("ဆီးချို သွေးချို အတွက်", "diabetes"),     # for diabetes
    ("ဝမ်းပျက် ဝမ်းလျှော အတွက်", "ors"),         # diarrhea -> ORS
    ("ခေါင်းကိုက် နာကျင်မှု ပျောက်ဆေး", "fever"), # headache/pain relief
    ("ကလေး နို့မှုန့်", "milk"),                 # baby milk powder
]
CANDS = {
    "fever": "PARACAP Paracetamol fever and pain relief ဖျားနာ",
    "diabetes": "Metformin blood sugar control diabetes ဆီးချို",
    "ors": "ELECTRAL ORS oral rehydration salts diarrhea ဝမ်းလျှော",
    "milk": "DUMEX baby milk powder infant nutrition ကလေးနို့မှုန့်",
    "wheelchair": "MEDICARE wheel chair mobility aid",
}
MODELS = ["google/gemini-embedding-2", "openai/text-embedding-3-small"]


def embed(client, model, text):
    t = time.time()
    r = client.post(URL, headers={"Authorization": f"Bearer {KEY}"},
                    json={"model": model, "input": text})
    r.raise_for_status()
    return r.json()["data"][0]["embedding"], time.time() - t


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / (math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b)))


with httpx.Client(timeout=60) as client:
    cand_vecs = {m: {} for m in MODELS}
    for m in MODELS:
        for k, txt in CANDS.items():
            cand_vecs[m][k], _ = embed(client, m, txt)

    for m in MODELS:
        correct, margins, lat = 0, [], []
        for query, want in TESTS:
            qv, dt = embed(client, m, query)
            lat.append(dt)
            ranked = sorted(((cos(qv, v), k) for k, v in cand_vecs[m].items()), reverse=True)
            if ranked[0][1] == want:
                correct += 1
            margins.append(ranked[0][0] - ranked[1][0])
        print(f"{m}")
        print(f"  correct={correct}/{len(TESTS)}  avg_margin={sum(margins)/len(margins):.3f}  "
              f"avg_query_latency={sum(lat)/len(lat)*1000:.0f}ms")
        print()
