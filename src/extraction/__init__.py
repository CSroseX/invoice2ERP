"""Extraction building blocks used by src/extractor.py.

- prompts.py         the LLM system prompt
- providers.py       LLM API clients, circuit breakers and the provider cascade
- postprocessing.py  deterministic business rules applied to the LLM output
- fallback.py        regex-based extractor used when no LLM is available
"""
