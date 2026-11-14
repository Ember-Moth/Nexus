from email_validator import EmailNotValidError, validate_email

from .errors import IdentityError


def mailbox(value: str) -> str:
    try:
        address = validate_email(value, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError:
        raise IdentityError("invalid_email", "A valid email address is required", 422) from None
    return address.ascii_email.lower()
