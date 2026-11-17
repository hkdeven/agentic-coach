#!/usr/bin/env python3
"""Price token counts at API list prices. Python 3 stdlib only.

The rates live in prices.json next to this file, with their source and date.
Dollars are an API-equivalent figure, not a bill: people on a subscription are
not charged them. A model that is not in the table is never guessed: its
tokens are reported as unpriced.

Token blocks are lists of five counts, always in this order:
  [input, output, cache read, cache write 5 minute, cache write 1 hour]
keyed by a model key: the model id from the transcript, plus "+long" for a
long-prompt request on a model with long-prompt prices, or "+fast" for fast mode.
"""
import json
import os
import re

TYPES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")
N = len(TYPES)
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATED_RE = re.compile(r"^(.+?)[-@]\d{8}$")
_cache = {}


def load_prices(path=None):
    path = path or os.path.join(_HERE, "prices.json")
    if path not in _cache:
        with open(path) as fh:
            _cache[path] = json.load(fh)
    return _cache[path]


def model_key(model, usage):
    """The key a request's tokens are counted under."""
    model = str(model or "unknown")
    if isinstance(usage, dict) and usage.get("speed") == "fast":
        return model + "+fast"
    entry = _entry(model, load_prices())
    long_prompt = entry.get("long_prompt") if entry else None
    if long_prompt and isinstance(usage, dict):
        prompt = (_n(usage.get("input_tokens")) + _n(usage.get("cache_read_input_tokens"))
                  + _n(usage.get("cache_creation_input_tokens")))
        if prompt > long_prompt.get("over_prompt_tokens", 0):
            return model + "+long"
    return model


def _n(v):
    """A token count from a transcript: a sane non-negative int, else 0."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 <= v < 1e12:
        return int(v)
    return 0


def usage_counts(usage):
    """A transcript usage object to the five counts."""
    write = _n(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    w1 = 0
    if isinstance(split, dict):
        w1 = min(write, _n(split.get("ephemeral_1h_input_tokens")))
    return [_n(usage.get("input_tokens")), _n(usage.get("output_tokens")),
            _n(usage.get("cache_read_input_tokens")), write - w1, w1]


def _entry(model, prices):
    models = prices.get("models") or {}
    if model in models:
        return models[model]
    m = _DATED_RE.match(model)
    if m and m.group(1) in models:
        return models[m.group(1)]
    return None


def rates(key, prices=None):
    """The five rates (USD per million tokens) for a model key, or None if unpriced."""
    prices = prices or load_prices()
    key = str(key)
    if key.endswith("+fast"):
        return None
    long_prompt = key.endswith("+long")
    entry = _entry(key[:-5] if long_prompt else key, prices)
    if entry is None:
        return None
    if long_prompt:
        entry = entry.get("long_prompt")
        if not entry:
            return None
    try:
        return [float(entry[t]) for t in TYPES]
    except (KeyError, TypeError, ValueError):
        return None


def display_name(key, prices=None):
    prices = prices or load_prices()
    key = str(key)
    base, _, flag = key.partition("+")
    entry = _entry(base, prices)
    name = entry.get("name", base) if entry else base
    if flag == "long":
        name += " (long prompt)"
    elif flag == "fast":
        name += " (fast mode)"
    return name


def counts(block):
    """Anything that should be five counts to five non-negative ints."""
    out = [0] * N
    if isinstance(block, (list, tuple)):
        for i, v in enumerate(block[:N]):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 <= v < 1e15:
                out[i] = int(v)
    return out


def add(into, block):
    block = counts(block)
    for i in range(N):
        into[i] += block[i]
    return into


def cost(by_model, prices=None):
    """Price a {model key: five counts} map.

    Returns {"usd", "by_type" (four dollar figures: input, output, cache_read,
    cache_write), "tokens" (five counts), "unpriced_tokens", "unpriced_models"}.
    """
    prices = prices or load_prices()
    usd = [0.0] * N
    tokens = [0] * N
    unpriced = 0
    unpriced_models = []
    for key in sorted(by_model or {}):
        block = counts(by_model[key])
        add(tokens, block)
        r = rates(key, prices)
        if r is None:
            if sum(block):
                unpriced += sum(block)
                unpriced_models.append(key)
            continue
        for i in range(N):
            usd[i] += block[i] * r[i] / 1e6
    return {"usd": round(sum(usd), 4),
            "by_type": {"input": round(usd[0], 4), "output": round(usd[1], 4),
                        "cache_read": round(usd[2], 4), "cache_write": round(usd[3] + usd[4], 4)},
            "tokens": tokens, "unpriced_tokens": unpriced, "unpriced_models": unpriced_models}


def fmt_usd(x):
    """Whole dollars from $10 up, cents below. None when there is nothing to show."""
    if x is None:
        return None
    x = max(0.0, float(x))
    if round(x, 2) >= 10:
        return "${:,.0f}".format(x)
    return "${:.2f}".format(x)


def fmt_tok(n):
    n = n or 0
    if n >= 999500000:
        return ("%.1fB" % (n / 1e9)).replace(".0B", "B")
    if n >= 999500:
        return ("%.1fM" % (n / 1e6)).replace(".0M", "M")
    if n >= 1000:
        return "%dk" % int(round(n / 1000.0))
    return "%d" % n


def prices_date(prices=None):
    return (prices or load_prices()).get("retrieved")
