"""Transactional e-mail through Amazon SES v2 (AA-811, SES identity ``lumiguides.it.com`` on acc2).

One function, :func:`send`, used by job handlers only (never from a request path). The task role may send
only as the Terraform ``ses_from_address`` (IAM condition ``ses:FromAddress``), so the From address comes from
settings and must match it exactly.

``boto3`` is imported lazily: the API process imports this module at start-up, and tests inject a stub client
(anything with ``send_email(**kwargs) -> {"MessageId": ...}``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol


class SesClient(Protocol):
    def send_email(self, **kwargs: Any) -> dict[str, Any]: ...


# SES answers that resending the same message can never fix (bad address, sender not allowed, account
# paused). Everything else (throttling, 5xx, network) is retried by the job runner.
PERMANENT_ERROR_CODES = frozenset(
    {
        "MessageRejected",
        "MailFromDomainNotVerifiedException",
        "AccountSuspendedException",
        "SendingPausedException",
        "BadRequestException",
        "NotFoundException",
        # AccessDeniedException is NOT here: right after the Infra apply that grants ses:SendEmail,
        # IAM propagation can deny the first sends; the runner's 5 backed-off attempts cover that.
    }
)


class MailError(Exception):
    """SES refused the message. ``permanent`` says whether a retry can help."""

    def __init__(self, code: str, message: str, *, permanent: bool) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.permanent = permanent


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str


def ses_client(region: str) -> SesClient:
    import boto3  # lazy: only the worker path that sends mail needs it

    return boto3.client("sesv2", region_name=region)


def _error_code(exc: BaseException) -> str | None:
    """The SES error code of a botocore ``ClientError`` (read duck-typed, so botocore is not imported)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str):
            return code
    return None


async def send(client: SesClient, message: Message, *, sender: str, configuration_set: str | None = None) -> str:
    """Send one message; returns the SES ``MessageId``. Raises :class:`MailError` on an SES refusal.

    The boto3 call is blocking, so it runs in a thread: the job worker shares the event loop with the API."""
    kwargs: dict[str, Any] = {
        "FromEmailAddress": sender,
        "Destination": {"ToAddresses": [message.to]},
        "Content": {
            "Simple": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": message.text, "Charset": "UTF-8"},
                    "Html": {"Data": message.html, "Charset": "UTF-8"},
                },
            }
        },
    }
    if configuration_set:
        kwargs["ConfigurationSetName"] = configuration_set
    try:
        response = await asyncio.to_thread(client.send_email, **kwargs)
    except Exception as exc:
        code = _error_code(exc)
        if code is None:
            raise
        raise MailError(code, str(exc)[:500], permanent=code in PERMANENT_ERROR_CODES) from exc
    return str(response["MessageId"])
