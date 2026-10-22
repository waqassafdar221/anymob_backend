"""Generate a private local configuration without overwriting an existing environment."""

import secrets
from pathlib import Path

from cryptography.fernet import Fernet

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit("backend/.env already exists; no changes made")
text = (root / ".env.example").read_text()
text = text.replace("replace-with-a-random-secret-at-least-32-characters", secrets.token_urlsafe(48))
text = text.replace("ENCRYPTION_KEY=\n", "ENCRYPTION_KEY=" + Fernet.generate_key().decode() + "\n")
target.write_text(text)
target.chmod(0o600)
print("Created backend/.env with independent secrets. Review service URLs before starting.")
