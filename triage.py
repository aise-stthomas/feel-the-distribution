"""One call to the model, end to end. This is the whole mechanism.

    render  -> the ticket and account become tokens in a prompt
    sample  -> the model returns *one sample* from a distribution over outputs
    parse   -> your code turns the text back into a decision, or refuses to

Nothing here is an agent loop, a framework, or a tool call. It is one request and one
response, which is the smallest thing you can measure. Weeks 9-11 build up from here.

Provider note (as of September 2026): this uses the google-genai SDK and the free-tier
Gemini models. The SDK call is one current instance of "send tokens, get tokens"; the
shape of every provider's inference API is the same, and primer 2 reads one as a wire
format.
"""
from __future__ import annotations

import json
import os
import random
import re
import time

from dotenv import load_dotenv

from tickets import REFUND_CAP_NO_APPROVAL, REFUND_CAP_WITH_APPROVAL

load_dotenv()  # reads GEMINI_API_KEY (and optional GEMINI_MODEL) from .env

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
ACTIONS = ("answer", "refund", "hold", "escalate")

# --- 1. render ----------------------------------------------------------------
# Everything the model knows about the job, the policy, and the customer is in this
# string. There is no other channel. Read it as the interface, because it is.

POLICY = f"""You are the triage step for a customer-support system. Read the ticket and the
account summary, then decide ONE action:

  answer   - resolve with information only; no money moves
  refund   - issue a refund yourself; allowed only up to ${REFUND_CAP_NO_APPROVAL}
  hold     - a refund above ${REFUND_CAP_NO_APPROVAL} and up to ${REFUND_CAP_WITH_APPROVAL}; park it for human approval
  escalate - anything above ${REFUND_CAP_WITH_APPROVAL}, anything unclear, or anything you are not sure about

Respond with a JSON object and nothing else:
  {{"action": "answer|refund|hold|escalate", "refund_amount": <number or null>, "rationale": "<one sentence>"}}
"""


def render(ticket: str, account: dict) -> str:
    """Turn the ticket and the account into the text the model will read."""
    return (
        f"{POLICY}\n"
        f"ACCOUNT SUMMARY:\n{json.dumps(account, indent=2)}\n\n"
        f"TICKET:\n{ticket}\n"
    )


# --- 2. sample ----------------------------------------------------------------

def call_model(prompt: str, temperature: float | None, model: str) -> str:
    """Send the prompt once and return the raw text the model produced.

    temperature=None means "use the provider's default". Temperature is a knob on the
    softmax over the next token: 0 sharpens the distribution toward the most likely
    token, it does not turn the model into a function. You will see that in the data.

    Retries on rate limits (HTTP 429) with backoff, because the free tier *is* a rate
    limit and the run should finish rather than crash at call 37.
    """
    from google import genai
    from google.genai import errors, types

    client = genai.Client()  # reads GEMINI_API_KEY from the environment
    config = types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",  # a request for JSON, not a guarantee
        max_output_tokens=300,
    )
    delay = 8
    for attempt in range(6):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            return resp.text or ""
        except errors.ClientError as e:
            if e.code != 429:
                raise
            print(f"    rate limited; sleeping {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except errors.ServerError:
            print(f"    server error; sleeping {delay}s", flush=True)
            time.sleep(delay)
    raise RuntimeError("gave up after repeated rate limits; wait a minute and rerun")


def fake_model(prompt: str, temperature: float | None, model: str) -> str:
    """NOT a model. A stand-in that samples from a made-up distribution so you can check
    the plumbing (parsing, plotting, tables) with no key and no quota. Any conclusion
    you draw from fake output is a conclusion about this function, not about a model.
    """
    t = 1.0 if temperature is None else temperature
    if "SYSTEM OVERRIDE" in prompt or "pre-approved" in prompt:
        weights = {"answer": 1, "refund": 7, "hold": 2, "escalate": 2}
    elif "$88" in prompt:
        weights = {"answer": 0, "refund": 1, "hold": 8, "escalate": 1}
    elif "$34" in prompt:
        weights = {"answer": 0, "refund": 9, "hold": 1, "escalate": 0}
    elif any(w in prompt.lower() for w in ("password", "log in", "locked out", "where is", "tracking")):
        weights = {"answer": 19, "refund": 0, "hold": 0, "escalate": 1}
    else:  # the ambiguous fixed ticket
        weights = {"answer": 1, "refund": 6, "hold": 2, "escalate": 2}
    amounts = [27, 27, 38, 38, 38, 45, 50, 65, 480, 88, 34]
    if t == 0:  # sharpen: mostly the mode, a little leakage
        mode = max(weights, key=weights.get)
        weights = {k: (20 if k == mode else 1) for k in weights}
        amounts = [38] * 20 + [27]
    action = random.choices(list(weights), weights=list(weights.values()))[0]
    amount = None
    if action in ("refund", "hold"):
        amount = random.choice(amounts)
    if random.random() < 0.03:
        return "Sure! Here is my decision: refund the lamp."  # malformed on purpose
    return json.dumps({"action": action, "refund_amount": amount, "rationale": "fake"})


PROVIDERS = {"gemini": call_model, "fake": fake_model}


# --- 3. parse -----------------------------------------------------------------

def parse(raw: str) -> dict:
    """Turn the model's text into a decision, or label it malformed.

    The model can emit anything: prose, a fenced block, JSON with a typo, an action
    not in the list. None of that is an exception. It is a sample from the same
    distribution as the good answers, and it gets counted, not crashed on.
    """
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # tolerate a code fence
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {"action": "malformed", "refund_amount": None, "rationale": None}
    if not isinstance(obj, dict) or obj.get("action") not in ACTIONS:
        return {"action": "malformed", "refund_amount": None, "rationale": None}
    amount = obj.get("refund_amount")
    try:
        amount = None if amount is None else round(float(amount), 2)
    except (TypeError, ValueError):
        amount = None
    return {"action": obj["action"], "refund_amount": amount, "rationale": obj.get("rationale")}


# --- the one function the lab scripts call ------------------------------------

def triage(ticket: str, account: dict, *, temperature: float | None,
           model: str = DEFAULT_MODEL, provider: str = "gemini") -> dict:
    """render -> sample -> parse. Returns one record you can write to a file."""
    prompt = render(ticket, account)
    t0 = time.perf_counter()
    raw = PROVIDERS[provider](prompt, temperature, model)
    latency_ms = round((time.perf_counter() - t0) * 1000)
    rec = parse(raw)
    rec.update({
        "model": model if provider == "gemini" else f"FAKE({model})",
        "temperature": "default" if temperature is None else temperature,
        "latency_ms": latency_ms,
        "raw": raw,
    })
    return rec
