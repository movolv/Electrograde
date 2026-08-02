"""DeepL connector — prepared (connect + test_connection + translate) but
not wired into any workflow yet. Reachable via
IntegrationManager.get(company_id, "deepl").translate(...) once a future
feature (e.g. auto-translating product descriptions) calls it; nothing in
app.py does today.

API docs: https://developers.deepl.com/docs/api-reference/translate
          https://developers.deepl.com/docs/api-reference/usage

DeepL has two API hosts depending on account type: a free-tier key always
ends in ":fx" and must use api-free.deepl.com; anything else (Pro) uses
api.deepl.com. Detected automatically from the key itself so the admin
never has to know or select this.

Expected shape: credentials = {"api_key": "..."}
"""
from typing import Optional

import requests

from integrations.base import ConnectionTestResult, ServiceConnector


def _api_base(api_key: str) -> str:
    return "https://api-free.deepl.com" if api_key.strip().endswith(":fx") else "https://api.deepl.com"


class DeepLConnector(ServiceConnector):
    integration_type = "deepl"

    def _headers(self) -> dict:
        return {"Authorization": f"DeepL-Auth-Key {self.credentials.get('api_key', '')}"}

    def connect(self) -> ConnectionTestResult:
        if not self.credentials.get("api_key"):
            return ConnectionTestResult(False, "API key is required.")
        return ConnectionTestResult(True, "Configuration looks valid.")

    def test_connection(self) -> ConnectionTestResult:
        pre = self.connect()
        if not pre.success:
            return pre
        api_key = self.credentials["api_key"]
        try:
            resp = requests.get(f"{_api_base(api_key)}/v2/usage", headers=self._headers(), timeout=15)
        except requests.RequestException as e:
            return ConnectionTestResult(False, f"Could not reach DeepL: {e}")
        if resp.status_code == 403:
            return ConnectionTestResult(False, "API key was rejected by DeepL.")
        if not resp.ok:
            return ConnectionTestResult(False, f"DeepL returned HTTP {resp.status_code}.")
        usage = resp.json()
        used = usage.get("character_count", "?")
        limit = usage.get("character_limit", "?")
        return ConnectionTestResult(True, f"Connected — {used}/{limit} characters used this period.")

    def translate(self, text: str, target_lang: str, source_lang: Optional[str] = None) -> str:
        api_key = self.credentials.get("api_key", "")
        if not api_key:
            raise RuntimeError("DeepL API key is not configured.")
        payload = {"text": [text], "target_lang": target_lang.upper()}
        if source_lang:
            payload["source_lang"] = source_lang.upper()
        resp = requests.post(f"{_api_base(api_key)}/v2/translate", headers=self._headers(), json=payload, timeout=20)
        resp.raise_for_status()
        translations = resp.json().get("translations", [])
        if not translations:
            raise RuntimeError("DeepL returned no translation.")
        return translations[0]["text"]
