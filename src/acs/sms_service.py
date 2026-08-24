"""
SMS Service for ARTAgent
========================

Reusable SMS service that can be used by any tool to send text messages via Azure Communication Services SMS.
Supports delivery reports and custom tagging for message tracking.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from utils.azure_auth import get_credential, should_use_managed_identity_for_acs
from utils.ml_logging import get_logger

# SMS service imports
try:
    from azure.communication.sms import SmsClient

    AZURE_SMS_AVAILABLE = True
except ImportError:
    AZURE_SMS_AVAILABLE = False

logger = get_logger("sms_service")


class SmsService:
    """Reusable SMS service for ARTAgent tools."""

    def __init__(self):
        """Initialize the SMS service with Azure configuration."""
        self.provider = os.getenv("SMS_PROVIDER", "auto").strip().lower() or "auto"
        self.connection_string = os.getenv(
            "AZURE_COMMUNICATION_SMS_CONNECTION_STRING"
        ) or os.getenv("ACS_CONNECTION_STRING")
        self.endpoint = os.getenv("ACS_ENDPOINT")
        self.from_phone_number = os.getenv("AZURE_SMS_FROM_PHONE_NUMBER")
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.twilio_from_phone_number = (
            os.getenv("TWILIO_FROM_PHONE_NUMBER")
            or os.getenv("TWILIO_SMS_FROM_NUMBER")
            or os.getenv("TWILIO_FROM_NUMBER")
            or os.getenv("TWILIO_PHONE_NUMBER")
            or ""
        )
        self.twilio_api_base_url = os.getenv(
            "TWILIO_API_BASE_URL", "https://api.twilio.com"
        ).rstrip("/")
        self.twilio_status_callback_url = os.getenv("TWILIO_SMS_STATUS_CALLBACK_URL", "")
        # Pre-create the SMS client once (avoid per-call overhead)
        self._sms_client: SmsClient | None = None
        if (
            AZURE_SMS_AVAILABLE
            and self.endpoint
            and should_use_managed_identity_for_acs()
        ):
            try:
                self._sms_client = SmsClient(self.endpoint, get_credential())
            except Exception as exc:
                logger.warning("Failed to pre-create SmsClient: %s", exc)
        elif AZURE_SMS_AVAILABLE and self.connection_string:
            try:
                self._sms_client = SmsClient.from_connection_string(self.connection_string)
            except Exception as exc:
                logger.warning("Failed to pre-create SmsClient: %s", exc)

    def is_configured(self) -> bool:
        """Check if SMS service is properly configured."""
        return self._select_provider() is not None

    def _is_acs_configured(self) -> bool:
        return (
            AZURE_SMS_AVAILABLE
            and self._sms_client is not None
            and bool(self.from_phone_number)
        )

    def _is_twilio_configured(self) -> bool:
        return all(
            [
                self.twilio_account_sid,
                self.twilio_auth_token,
                self.twilio_from_phone_number,
            ]
        )

    def _select_provider(self) -> str | None:
        if self.provider == "twilio":
            return "twilio" if self._is_twilio_configured() else None
        if self.provider == "acs":
            return "acs" if self._is_acs_configured() else None
        if self._is_acs_configured():
            return "acs"
        if self._is_twilio_configured():
            return "twilio"
        return None

    async def send_sms(
        self,
        to_phone_numbers: str | list[str],
        message: str,
        enable_delivery_report: bool = True,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> dict[str, Any]:
        """
        Send SMS using Azure Communication Services SMS.

        Args:
            to_phone_numbers: Recipient phone number(s) - can be single string or list
            message: SMS message content
            enable_delivery_report: Whether to enable delivery reports
            tag: Optional tag for message tracking

        Returns:
            Dict containing success status, message IDs, and error details if any
        """
        try:
            provider = self._select_provider()
            if provider is None:
                return {
                    "success": False,
                    "error": "SMS service not configured or not available",
                    "sent_messages": [],
                }
            if provider == "twilio":
                return await self._send_twilio_sms(
                    to_phone_numbers=to_phone_numbers,
                    message=message,
                    tag=tag,
                    status_callback_url=status_callback_url,
                )

            # Ensure phone numbers is a list
            if isinstance(to_phone_numbers, str):
                to_phone_numbers = [to_phone_numbers]

            # Use pre-created SMS client (falls back to creating one if needed)
            client = self._sms_client
            if client is None:
                client = SmsClient.from_connection_string(self.connection_string)

            # Offload blocking SDK call to thread pool
            def _blocking_send():
                return client.send(
                    from_=self.from_phone_number,
                    to=to_phone_numbers,
                    message=message,
                    enable_delivery_report=enable_delivery_report,
                    tag=tag or "ARTAgent SMS",
                )

            sms_responses = await asyncio.to_thread(_blocking_send)

            # Process responses
            sent_messages = []
            failed_messages = []

            for response in sms_responses:
                message_data = {
                    "to": response.to,
                    "message_id": response.message_id,
                    "http_status_code": response.http_status_code,
                    "successful": response.successful,
                    "error_message": (
                        response.error_message if hasattr(response, "error_message") else None
                    ),
                }

                if response.successful:
                    sent_messages.append(message_data)
                    logger.info("SMS accepted by Azure Communication Services")
                else:
                    failed_messages.append(message_data)
                    logger.error(
                        "SMS rejected by Azure Communication Services: %s",
                        (
                            response.error_message
                            if hasattr(response, "error_message")
                            else "Unknown error"
                        ),
                    )

            return {
                "success": len(failed_messages) == 0,
                "sent_count": len(sent_messages),
                "failed_count": len(failed_messages),
                "sent_messages": sent_messages,
                "failed_messages": failed_messages,
                "service": "Azure Communication Services SMS",
                "tag": tag or "ARTAgent SMS",
            }

        except Exception as exc:
            logger.warning(
                "SMS provider request raised %s; outcome requires reconciliation",
                type(exc).__name__,
            )
            return {
                "success": False,
                "error": "provider_outcome_unknown",
                "error_class": type(exc).__name__,
                "outcome_unknown": True,
                "sent_messages": [],
                "failed_messages": [],
            }

    async def _send_twilio_sms(
        self,
        to_phone_numbers: str | list[str],
        message: str,
        tag: str | None = None,
        status_callback_url: str | None = None,
    ) -> dict[str, Any]:
        """Send SMS through Twilio Programmable Messaging for Phase 0 fallback."""
        import httpx

        if isinstance(to_phone_numbers, str):
            to_phone_numbers = [to_phone_numbers]

        sent_messages = []
        failed_messages = []
        url = f"{self.twilio_api_base_url}/2010-04-01/Accounts/{self.twilio_account_sid}/Messages.json"

        async with httpx.AsyncClient(timeout=15.0) as client:
            for to_phone_number in to_phone_numbers:
                data = {
                    "From": self.twilio_from_phone_number,
                    "To": to_phone_number,
                    "Body": message,
                }
                callback_url = status_callback_url or self.twilio_status_callback_url
                if callback_url:
                    data["StatusCallback"] = callback_url

                response = await client.post(
                    url,
                    data=data,
                    auth=(self.twilio_account_sid, self.twilio_auth_token),
                )
                response_data = _safe_json(response)
                message_data = {
                    "to": to_phone_number,
                    "message_id": response_data.get("sid"),
                    "http_status_code": response.status_code,
                    "successful": 200 <= response.status_code < 300,
                    "error_message": response_data.get("message"),
                    "error_code": response_data.get("code"),
                }

                if message_data["successful"]:
                    sent_messages.append(message_data)
                    logger.info("SMS accepted by Twilio")
                else:
                    failed_messages.append(message_data)
                    logger.error(
                        "SMS rejected by Twilio: %s",
                        message_data["error_code"] or response.status_code,
                    )

        return {
            "success": len(failed_messages) == 0,
            "sent_count": len(sent_messages),
            "failed_count": len(failed_messages),
            "sent_messages": sent_messages,
            "failed_messages": failed_messages,
            "service": "Twilio Programmable Messaging",
            "provider": "twilio",
            "tag": tag or "ARTAgent SMS",
        }

    def send_sms_background(
        self,
        to_phone_numbers: str | list[str],
        message: str,
        enable_delivery_report: bool = True,
        tag: str | None = None,
        callback: callable | None = None,
    ) -> None:
        """
        Send SMS in background thread without blocking the main response.

        Args:
            to_phone_numbers: Recipient phone number(s) - can be single string or list
            message: SMS message content
            enable_delivery_report: Whether to enable delivery reports
            tag: Optional tag for message tracking
            callback: Optional callback function to handle the result
        """

        def _send_sms_background_task():
            try:
                # Create new event loop for background task
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                # Send the SMS
                result = loop.run_until_complete(
                    self.send_sms(to_phone_numbers, message, enable_delivery_report, tag)
                )

                # Log result
                if result.get("success"):
                    logger.info(
                        "📱 Background SMS sent successfully: %d messages",
                        result.get("sent_count", 0),
                    )
                else:
                    logger.warning("📱 Background SMS failed: %s", result.get("error"))

                # Call callback if provided
                if callback:
                    callback(result)

            except Exception as exc:
                logger.error("Background SMS task failed: %s", exc, exc_info=True)
            finally:
                loop.close()

        try:
            sms_thread = threading.Thread(target=_send_sms_background_task, daemon=True)
            sms_thread.start()
            logger.info("📱 SMS sending started in background thread")
        except Exception as exc:
            logger.error("Failed to start background SMS thread: %s", exc)


# Global SMS service instance
sms_service = SmsService()


# Convenience functions for easy import
async def send_sms(
    to_phone_numbers: str | list[str],
    message: str,
    enable_delivery_report: bool = True,
    tag: str | None = None,
) -> dict[str, Any]:
    """Convenience function to send SMS."""
    return await sms_service.send_sms(to_phone_numbers, message, enable_delivery_report, tag)


def send_sms_background(
    to_phone_numbers: str | list[str],
    message: str,
    enable_delivery_report: bool = True,
    tag: str | None = None,
    callback: callable | None = None,
) -> None:
    """Convenience function to send SMS in background."""
    sms_service.send_sms_background(
        to_phone_numbers, message, enable_delivery_report, tag, callback
    )


def is_sms_configured() -> bool:
    """Check if SMS service is configured."""
    return sms_service.is_configured()


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
