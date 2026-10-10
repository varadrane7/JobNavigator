"""Per-tenant ATS logins, encrypted at rest.

The Fernet key comes from AUTOAPPLY_SECRET_KEY, or else a file next to the other AutoApply data
(created 0600 on first use). Keeping it outside the database means a DB dump alone does not
expose the passwords."""
import os
import secrets
import string
from datetime import datetime, timezone

from backend.autoapply.models import AutoApplyCredential


def _key(data_dir: str) -> bytes:
    env = os.environ.get("AUTOAPPLY_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    from cryptography.fernet import Fernet
    path = os.path.join(data_dir, ".secret_key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    os.makedirs(data_dir, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _fernet(data_dir: str):
    from cryptography.fernet import Fernet
    return Fernet(_key(data_dir))


def new_password() -> str:
    """Meets the usual Workday rules: upper, lower, digit, symbol, 20 chars."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!#$%*+-=?@^_"]
    chars = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(16)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def save(db, data_dir: str, host: str, email: str, password: str, created_by: str = "autoapply") -> None:
    token = _fernet(data_dir).encrypt(password.encode()).decode()
    row = db.query(AutoApplyCredential).filter(AutoApplyCredential.host == host).first()
    if row:
        row.email, row.password_enc, row.created_by = email, token, created_by
    else:
        db.add(AutoApplyCredential(host=host, email=email, password_enc=token, created_by=created_by))
    db.commit()


def load(db, data_dir: str, host: str):
    """(email, password) for the tenant, or None."""
    row = db.query(AutoApplyCredential).filter(AutoApplyCredential.host == host).first()
    if not row:
        return None
    row.last_used_at = datetime.now(timezone.utc)
    db.commit()
    return row.email, _fernet(data_dir).decrypt(row.password_enc.encode()).decode()


def listing(db) -> list[dict]:
    return [{"host": r.host, "email": r.email, "created_by": r.created_by,
             "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None}
            for r in db.query(AutoApplyCredential).order_by(AutoApplyCredential.host).all()]


def delete(db, host: str) -> bool:
    n = db.query(AutoApplyCredential).filter(AutoApplyCredential.host == host).delete()
    db.commit()
    return bool(n)
