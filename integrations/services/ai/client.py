"""OpenAI connector — a company's own OpenAI account, brought for optional
workflows outside ElectroGrader's own core AI features.

Distinct from modules/ai_client.py, which is ElectroGrader's own built-in
Anthropic-powered grading/description assistant and is NOT going through
this integration framework — it's core product functionality, not an
optional per-company connection.

First (and so far only) capability: translate() — an alternative to
DeepLConnector (integrations/services/deepl/client.py) for
modules/translation_service.py's provider abstraction. Same
ServiceConnector shape as DeepL: connect() validates the key is present,
test_connection() makes one cheap real API call, translate() does the
actual work. process()/calculate() stay unimplemented (base class default)
until a concrete use for them exists.

Expected shape: credentials = {"api_key": "..."}
"""
import requests

from integrations.base import ConnectionTestResult, ServiceConnector

_API_BASE = "https://api.openai.com/v1"
_MODEL = "gpt-4o-mini"


class AiServiceConnector(ServiceConnector):
    integration_type = "openai"

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.credentials.get('api_key', '')}",
            "Content-Type": "application/json",
        }

    def connect(self) -> ConnectionTestResult:
        if not self.credentials.get("api_key"):
            return ConnectionTestResult(False, "API key is required.")
        return ConnectionTestResult(True, "Configuration looks valid.")

    def test_connection(self) -> ConnectionTestResult:
        pre = self.connect()
        if not pre.success:
            return pre
        try:
            resp = requests.get(f"{_API_BASE}/models", headers=self._headers(), timeout=15)
        except requests.RequestException as e:
            return ConnectionTestResult(False, f"Could not reach OpenAI: {e}")
        if resp.status_code == 401:
            return ConnectionTestResult(False, "API key was rejected by OpenAI.")
        if not resp.ok:
            return ConnectionTestResult(False, f"OpenAI returned HTTP {resp.status_code}.")
        return ConnectionTestResult(True, "Connected — API key is valid.")

    def translate(self, text: str, source_language: str, target_language: str) -> str:
        api_key = self.credentials.get("api_key", "")
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured.")
        payload = {
            "model": _MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Translate the given text from {source_language} to {target_language}. "
                        "Preserve meaning, tone, and formatting exactly. Respond with ONLY the "
                        "translated text — no quotes, no explanation, no preamble."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
        }
        resp = requests.post(f"{_API_BASE}/chat/completions", headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        choices = resp.json().get("choices", [])
        if not choices:
            raise RuntimeError("OpenAI returned no translation.")
        return choices[0]["message"]["content"].strip()
