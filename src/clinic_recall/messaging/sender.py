"""Message sender adapters for Clinic Recall outreach."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..durable.callbacks import parse_effect_token

T = TypeVar("T")


class ProviderOutcomeUnknownError(RuntimeError):
    """Raised when a provider request may have succeeded without a response."""


class ProviderFailureCode(StrEnum):
    """Closed failure classes derived only from structured provider evidence."""

    RATE_LIMITED = "provider_rate_limited"
    SERVICE_UNAVAILABLE = "provider_service_unavailable"
    PERMANENT_REJECTION = "provider_rejected"


RETRYABLE_PROVIDER_FAILURES = frozenset(
    {ProviderFailureCode.RATE_LIMITED, ProviderFailureCode.SERVICE_UNAVAILABLE}
)


@dataclass(frozen=True)
class SendResult:
    """Provider-agnostic result for a single outbound message attempt."""

    successful: bool
    provider: str
    provider_message_id: str | None = None
    http_status_code: int | None = None
    error: str | None = None


def classify_provider_failure(result: SendResult) -> ProviderFailureCode | None:
    """Classify a returned provider response without inspecting free-form text."""
    if result.successful:
        return None
    if result.http_status_code == 429:
        return ProviderFailureCode.RATE_LIMITED
    if result.http_status_code in {502, 503}:
        return ProviderFailureCode.SERVICE_UNAVAILABLE
    return ProviderFailureCode.PERMANENT_REJECTION


@dataclass(frozen=True)
class SmsMessage:
    """Message captured by the fake sender for offline tests and demos."""

    to: str
    body: str
    tag: str | None
    status_callback_url: str | None = None


@dataclass(frozen=True)
class EmailMessage:
    """Email captured by the fake sender for offline tests and demos."""

    to: str
    subject: str
    body: str
    html_body: str | None


@runtime_checkable
class MessageSender(Protocol):
    """Synchronous messaging interface used by the deterministic orchestrator."""

    name: str

    def send_sms(
        self,
        *,
        to: str,
        body: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> SendResult:
        """Send one SMS message."""
        ...

    def send_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> SendResult:
        """Send one email message."""
        ...


def _run_blocking(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async ART primitive from sync Clinic Recall worker code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, T] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            error["value"] = exc

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if error:
        raise error["value"]
    return result["value"]


def _sms_result(raw: dict[str, Any], provider: str) -> SendResult:
    sent = list(raw.get("sent_messages") or [])
    failed = list(raw.get("failed_messages") or [])
    first = sent[0] if sent else failed[0] if failed else {}
    return SendResult(
        successful=bool(raw.get("success")),
        provider=str(raw.get("service") or provider),
        provider_message_id=first.get("message_id"),
        http_status_code=first.get("http_status_code"),
        error=str(raw.get("error") or first.get("error_message") or "") or None,
    )


class AcsSmsSender:
    """SMS adapter over the existing ART ACS SMS service."""

    name = "acs_sms"

    def __init__(self, service: Any | None = None) -> None:
        if service is None:
            from src.acs.sms_service import SmsService

            service = SmsService()
        self._service = service

    def send_sms(
        self,
        *,
        to: str,
        body: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> SendResult:
        raw = _run_blocking(
            self._service.send_sms(
                to_phone_numbers=to,
                message=body,
                tag=tag,
                status_callback_url=status_callback_url,
            )
        )
        if raw.get("outcome_unknown"):
            raise ProviderOutcomeUnknownError("SMS provider outcome is unknown")
        return _sms_result(raw, self.name)

    def send_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> SendResult:
        return SendResult(successful=False, provider=self.name, error="email_not_supported")


class TwilioSmsSender(AcsSmsSender):
    """SMS adapter over the ART Twilio fallback primitive."""

    name = "twilio_sms"

    def send_sms(
        self,
        *,
        to: str,
        body: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> SendResult:
        raw = _run_blocking(
            self._service._send_twilio_sms(
                to_phone_numbers=to,
                message=body,
                tag=tag,
                status_callback_url=status_callback_url,
            )
        )
        return _sms_result(raw, self.name)


class AcsEmailSender:
    """Email adapter over the existing ART ACS email service."""

    name = "acs_email"

    def __init__(self, service: Any | None = None) -> None:
        if service is None:
            from src.acs.email_service import EmailService

            service = EmailService()
        self._service = service

    def send_sms(
        self,
        *,
        to: str,
        body: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> SendResult:
        return SendResult(successful=False, provider=self.name, error="sms_not_supported")

    def send_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> SendResult:
        raw = _run_blocking(
            self._service.send_email(
                email_address=to,
                subject=subject,
                plain_text_body=body,
                html_body=html_body,
            )
        )
        return SendResult(
            successful=bool(raw.get("success")),
            provider=str(raw.get("service") or self.name),
            provider_message_id=raw.get("message_id"),
            error=str(raw.get("error") or "") or None,
        )


class FakeMessageSender:
    """Offline sender used by tests and demos."""

    name = "fake"

    def __init__(self, *, sms_success: bool = True, email_success: bool = True) -> None:
        self.sms_success = sms_success
        self.email_success = email_success
        self.sms_messages: list[SmsMessage] = []
        self.email_messages: list[EmailMessage] = []

    def send_sms(
        self,
        *,
        to: str,
        body: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> SendResult:
        self.sms_messages.append(
            SmsMessage(
                to=to,
                body=body,
                tag=tag,
                status_callback_url=status_callback_url,
            )
        )
        message_id = f"fake-sms-{len(self.sms_messages)}"
        return SendResult(
            successful=self.sms_success,
            provider=self.name,
            provider_message_id=message_id if self.sms_success else None,
            error=None if self.sms_success else "fake_sms_failure",
        )

    def send_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        html_body: str | None = None,
    ) -> SendResult:
        self.email_messages.append(
            EmailMessage(to=to, subject=subject, body=body, html_body=html_body)
        )
        message_id = f"fake-email-{len(self.email_messages)}"
        return SendResult(
            successful=self.email_success,
            provider=self.name,
            provider_message_id=message_id if self.email_success else None,
            error=None if self.email_success else "fake_email_failure",
        )


def twilio_sms_status_callback_url(effect_token: str) -> str | None:
    """Build the exact message-specific callback URL for one durable effect."""
    parse_effect_token(effect_token)
    explicit = os.getenv("TWILIO_SMS_STATUS_CALLBACK_URL", "").strip()
    if explicit:
        callback_url = explicit
    else:
        base_url = (
            os.getenv("TWILIO_WEBHOOK_BASE_URL", "").strip()
            or os.getenv("BASE_URL", "").strip()
        )
        if not base_url:
            return None
        callback_url = f"{base_url.rstrip('/')}/api/v1/sms/twilio"

    parts = urlsplit(callback_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Twilio SMS callback URL must be an absolute HTTP(S) URL")
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "effect_token" for key, _value in query):
        raise ValueError("Twilio SMS callback URL must not predefine effect_token")
    query.append(("effect_token", effect_token))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            "",
        )
    )