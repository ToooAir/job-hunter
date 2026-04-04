"""
check_api.py — Quick connectivity check for LLM + embedding APIs.
Usage: python check_api.py
"""

import sys
from dotenv import load_dotenv

load_dotenv()

from utils.llm import make_client, chat_model, emb_model, LLM_PROVIDER, rate_limit

print(f"Provider : {LLM_PROVIDER}")
print(f"Chat model: {chat_model()}")
print(f"Emb model : {emb_model()}")
print()

client = make_client()
ok = True

# ── Chat ──────────────────────────────────────────────────────────────────────
print("[ Chat ]", end=" ", flush=True)
try:
    rate_limit()
    resp = client.chat.completions.create(
        model=chat_model(),
        messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        max_tokens=5,
        temperature=0,
    )
    reply = resp.choices[0].message.content.strip()
    print(f"✓  ({reply})")
except Exception as e:
    print(f"✗  {e}")
    ok = False

# ── Embedding ─────────────────────────────────────────────────────────────────
print("[ Embed ]", end=" ", flush=True)
try:
    rate_limit()
    resp = client.embeddings.create(model=emb_model(), input=["test"])
    dim = len(resp.data[0].embedding)
    print(f"✓  dim={dim}")
except Exception as e:
    print(f"✗  {e}")
    ok = False

sys.exit(0 if ok else 1)
