#!/usr/bin/env python3
"""
Compare query rewrites from 3 small instruction-tuned models, run locally
via transformers (CPU-friendly). Useful for picking a model + prompt before
wiring rewriting into your retrieval pipeline.

Usage:
    python rewrite_query.py -Q "cheap apartments venice" \
        --context "Domain: Italian municipal planning documents. \
Prefer bureaucratic terminology, e.g. PRG, variante, vincolo paesaggistico, \
edilizia residenziale pubblica, contributo affitto."

Notes:
- First run downloads the models (a few hundred MB to ~1.5GB each) and
  caches them under ~/.cache/huggingface.
- All three run on CPU. Expect a few seconds per model for short prompts.
- Swap MODELS below for whichever small instruct checkpoints you want to
  compare - these three are just a reasonable, distinct-size starting set.
"""

import argparse
import time

from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

MODELS = [
    "Qwen/Qwen2.5-0.5B-Instruct",
    "Qwen/Qwen2.5-1.5B-Instruct",
    "google/gemma-2-2b-it",
]

SYSTEM_PROMPT_TEMPLATE = (
    "You rewrite short search queries so they match the terminology used in "
    "a specific document corpus. Output ONLY the rewritten query, nothing else "
    "- no explanation, no quotes, no preamble.\n\n{context}"
)

DEFAULT_CONTEXT = (
    "Domain: general purpose. If unsure, keep the query close to the original."
)


def rewrite_with_model(model_id: str, query: str, context: str) -> tuple[str, float]:
    """Load a model, run one rewrite, return (rewritten_query, seconds_taken)."""
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(model_id)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Query: {query}"},
    ]

    # chat template handles model-specific formatting (Qwen/Gemma differ internally)
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    generator = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        device_map="cpu",
    )

    start = time.time()
    output = generator(
        prompt,
        max_new_tokens=64,
        do_sample=False,          # deterministic - you want reproducible rewrites
        temperature=None,
        top_p=None,
        pad_token_id=tokenizer.eos_token_id,
    )
    elapsed = time.time() - start

    full_text = output[0]["generated_text"]
    rewritten = full_text[len(prompt):].strip()
    return rewritten, elapsed


def main():
    parser = argparse.ArgumentParser(description="Compare small-LLM query rewrites")
    parser.add_argument("-Q", "--query", required=True, help="Original user query")
    parser.add_argument(
        "--context",
        default=DEFAULT_CONTEXT,
        help="Domain context / terminology hints injected into the system prompt",
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=MODELS,
        help="Override the list of HF model IDs to compare",
    )
    args = parser.parse_args()

    print(f"\nOriginal query: {args.query}\n")
    print(f"Context: {args.context}\n")
    print("-" * 70)

    for model_id in args.models:
        try:
            rewritten, elapsed = rewrite_with_model(model_id, args.query, args.context)
            print(f"[{model_id}]  ({elapsed:.2f}s)")
            print(f"  -> {rewritten}\n")
        except Exception as e:
            print(f"[{model_id}] FAILED: {e}\n")

    print("-" * 70)


if __name__ == "__main__":
    main()
