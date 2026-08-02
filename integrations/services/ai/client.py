"""Not implemented yet — structural placeholder only.

Distinct from modules/ai_client.py, which is ElectroGrader's own built-in
Anthropic-powered grading/description assistant and is NOT going through
this integration framework — it's core product functionality, not an
optional per-company connection. This stub is for a future *external*
AI service a company could bring their own account for (e.g. a
company-owned OpenAI key for some other workflow). When built, this
becomes an `AiServiceConnector(ServiceConnector)` (see integrations/base.py)
overriding process() or calculate() as appropriate, registered in
integrations/manager.py's CONNECTORS dict.
"""
