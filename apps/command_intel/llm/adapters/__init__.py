"""Per-vendor LLM adapters. Each maps the neutral request/response to the vendor
wire format. ``minimax`` is the reference (OpenAI-compatible); others can be added
without touching the client or the prompts (design doc 11 §1, ADR-0003).
"""
