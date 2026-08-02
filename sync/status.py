"""Constants shared by the whole sync/ package — zero DB, zero Streamlit,
zero integrations/ dependency (this is the lowest layer; everything else
in sync/ imports from here, never the other way).
"""

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"  # honest "not implemented / not available" outcome, not a real failure

DIRECTION_EXPORT = "export"  # ElectroGrader -> marketplace
DIRECTION_IMPORT = "import"  # marketplace -> ElectroGrader
