import secrets

from ..models import User

# No 0/O/1/I/L — avoids transcription errors when a code is read aloud or
# typed from a screenshot. Not a secret, so no cryptographic-strength
# requirement, just uniqueness.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_referral_code(length=8):
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if not User.query.filter_by(referral_code=code).first():
            return code
