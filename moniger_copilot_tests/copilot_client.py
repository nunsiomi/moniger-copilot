"""
Moniger Copilot — Test Client Adapter
======================================
The ONLY file you should need to edit when the API contract changes.
Everything in test_copilot.py talks to Copilot through this adapter.

Adapted to the real /ask contract (see app/main.py, app/gating.py):
  - Auth is a bearer JWT, not an API key -- /ask 401s without one. Signed
    locally here with JWT_SECRET (must match whatever the API process
    itself reads from its own .env; both default to "dev-only-change-me"),
    claims {"sub": user_token, "tier": "premium"} -- "premium" so a 72-test
    run never trips the 5/month free-tier quota. This mirrors exactly how
    demo/components/chat.py signs its own local demo token.
  - The request body is {"question", "user_id"} -- the API's own field
    name is "user_id", not "userToken"; it holds the same identifier space
    as the mirror's userToken (see app/gating.py), so no other change is
    needed on the caller's side.
  - Transactions/budgets are never sent in the request body -- /ask looks
    them up server-side from the Mongo mirror by user_id (see
    app/data_access.py). This client sends only the question and identity.

Configure via environment variables:
    COPILOT_API_URL   e.g. https://xxxx.execute-api.eu-west-1.amazonaws.com/ask
    JWT_SECRET        must match the API process's own JWT_SECRET
    COPILOT_API_KEY   optional, sent as x-api-key (unused by app/main.py
                       today, but harmless to send if a gateway in front of
                       it expects one)
    COPILOT_TIMEOUT   seconds, default 45
"""

import os, re, json, time
import urllib.request, urllib.error

import jwt

API_URL = os.environ.get("COPILOT_API_URL", "").rstrip("/")
API_KEY = os.environ.get("COPILOT_API_KEY", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-change-me")
TIMEOUT = int(os.environ.get("COPILOT_TIMEOUT", "45"))


def _bearer_token(user_id):
    """Mint a short-lived local JWT for `user_id`, "premium" tier (see
    module docstring) -- exactly how demo/components/chat.py signs its own
    demo token, not a real login flow."""
    return jwt.encode({"sub": user_id, "tier": "premium"}, JWT_SECRET, algorithm="HS256")


class CopilotResponse:
    """Normalised view of whatever the API returns."""

    def __init__(self, raw):
        self.raw = raw
        if isinstance(raw, dict):
            self.answer      = str(raw.get("answer", "") or "")
            self.explanation = str(raw.get("explanation", "") or "")
            self.confidence  = raw.get("confidence")
            self.intent      = raw.get("intent") or raw.get("question_type")
            self.sources     = raw.get("sources", [])
        else:
            self.answer, self.explanation = str(raw), ""
            self.confidence = self.intent = None
            self.sources = []

    @property
    def text(self):
        return f"{self.answer}\n{self.explanation}".strip()

    def amounts(self):
        """
        Every naira figure mentioned, as floats.
        Handles: N45,000 / NGN45,000 / 45,000.00 / 45000
        """
        out = []
        for m in re.finditer(r"(?:\u20A6|NGN|N)?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)",
                             self.text):
            try:
                out.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
        return out

    def has_amount(self, expected, tolerance=0.02):
        """True if any figure in the response matches within tolerance (default 2%)."""
        for a in self.amounts():
            if expected == 0 and a == 0:
                return True
            if expected and abs(a - expected) <= abs(expected) * tolerance:
                return True
        return False

    def percentages(self):
        return [float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s?%", self.text)]

    def has_percentage(self, expected, tolerance=2.0):
        return any(abs(p - expected) <= tolerance for p in self.percentages())

    def __repr__(self):
        return f"<CopilotResponse intent={self.intent} text={self.text[:90]!r}>"


def configured():
    return bool(API_URL)


def ask(question, user_token, retries=2):
    """POST a question. Adjust the payload/parse here if the contract differs."""
    if not API_URL:
        raise RuntimeError("COPILOT_API_URL is not set")

    payload = json.dumps({"question": question, "user_id": user_token}).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_bearer_token(user_token)}",
    }
    if API_KEY:
        headers["x-api-key"] = API_KEY

    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return CopilotResponse(json.loads(r.read().decode()))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            last = RuntimeError(f"HTTP {e.code}: {body}")
            if e.code < 500:
                raise last
        except Exception as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise last
