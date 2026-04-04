"""
utils/llm.py — Shared OpenAI-compatible client factory.
Reads LLM_PROVIDER and related env vars; call make_client() / emb_model() anywhere.
"""

import os
import openai

LLM_PROVIDER          = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
AZURE_ENDPOINT        = os.getenv("AZURE_ENDPOINT", "")
AZURE_API_VERSION     = os.getenv("AZURE_API_VERSION", "2024-08-01-preview")
AZURE_CHAT_DEPLOYMENT = os.getenv("AZURE_CHAT_DEPLOYMENT", "gpt-4o")
AZURE_EMB_DEPLOYMENT  = os.getenv("AZURE_EMB_DEPLOYMENT", "text-embedding-3-small")
CUSTOM_BASE_URL       = os.getenv("CUSTOM_BASE_URL", "")
CHAT_MODEL            = os.getenv("CHAT_MODEL", "gpt-4o")
EMB_MODEL             = os.getenv("EMB_MODEL", "text-embedding-3-small")


def make_client() -> openai.OpenAI:
    if LLM_PROVIDER == "azure":
        return openai.AzureOpenAI(
            api_key=OPENAI_API_KEY,
            azure_endpoint=AZURE_ENDPOINT,
            api_version=AZURE_API_VERSION,
        )
    if LLM_PROVIDER == "custom":
        return openai.OpenAI(
            api_key=OPENAI_API_KEY or "dummy",
            base_url=CUSTOM_BASE_URL,
        )
    return openai.OpenAI(api_key=OPENAI_API_KEY)


def chat_model() -> str:
    return AZURE_CHAT_DEPLOYMENT if LLM_PROVIDER == "azure" else CHAT_MODEL


def emb_model() -> str:
    return AZURE_EMB_DEPLOYMENT if LLM_PROVIDER == "azure" else EMB_MODEL
