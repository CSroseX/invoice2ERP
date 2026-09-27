import os
import sys

# Force invalid keys for testing
os.environ["OPEN_ROUTER_API_KEY"] = "sk-or-v1-invalid_key"
os.environ["GROQ_API_KEY"] = "gsk_invalid_key"
os.environ["CLOUDFLARE_WORKERS_AI_KEY"] = "invalid"

# We must import after modifying environment, though python's os.environ might not affect already loaded variables if they are set at module level.
# In src/extractor.py they are read at module level:
# GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# So we need to modify them dynamically in the module after importing.

from src import extractor

extractor.OPEN_ROUTER_API_KEY = "sk-or-v1-invalid_key"
extractor.GROQ_API_KEY = "gsk_invalid_key"
extractor.CLOUDFLARE_WORKERS_AI_KEY = "invalid"

print("--- 1. Testing Circuit Breaker & Retries ---")
try:
    extractor.get_raw_llm_response("Test OCR text for circuit breaker", filename="test_cb.pdf")
except Exception as e:
    print(f"Final Exception Caught: {e}")

print("\n--- 2. Testing Circuit State (should fail fast) ---")
try:
    extractor.get_raw_llm_response("Test OCR text for circuit breaker 2", filename="test_cb2.pdf")
except Exception as e:
    print(f"Final Exception Caught: {e}")
