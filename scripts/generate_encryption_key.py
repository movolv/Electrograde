"""Prints a fresh Fernet key for ELECTROGRADER_ENCRYPTION_KEY (modules/
crypto.py). Run once per environment (dev machine, each deploy target) and
paste the output into that environment's .env — never share one key across
environments, and never commit the generated value.

    python scripts/generate_encryption_key.py
"""
from cryptography.fernet import Fernet


def main() -> int:
    print(Fernet.generate_key().decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
