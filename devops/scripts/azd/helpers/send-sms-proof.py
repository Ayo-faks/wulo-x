"""Send one ACS SMS for the Phase 0 spike proof."""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404
import sys

from azure.communication.sms import SmsClient


def _run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)  # nosec B603
    return result.stdout.strip()


def _azd_get(name: str) -> str:
    try:
        return _run(["azd", "env", "get-value", name])
    except Exception:
        return ""


def _get_connection_string() -> str:
    if os.getenv("ACS_CONNECTION_STRING"):
        return os.environ["ACS_CONNECTION_STRING"]

    key_vault = _azd_get("AZURE_KEY_VAULT_NAME")
    if not key_vault:
        raise RuntimeError("AZURE_KEY_VAULT_NAME is not available from azd env")

    return _run(
        [
            "az",
            "keyvault",
            "secret",
            "show",
            "--vault-name",
            key_vault,
            "--name",
            "acs-connection-string",
            "--query",
            "value",
            "-o",
            "tsv",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", default=os.getenv("PHASE0_SMS_TO"), help="Recipient E.164 number")
    parser.add_argument(
        "--from-number",
        default=os.getenv("ACS_SOURCE_PHONE_NUMBER") or _azd_get("ACS_SOURCE_PHONE_NUMBER"),
        help="ACS source E.164 number",
    )
    parser.add_argument(
        "--message",
        default="Clinic Recall Phase 0 SMS proof. Reply TEST to confirm webhook capture.",
        help="SMS message body",
    )
    args = parser.parse_args()

    if not args.to or not args.from_number:
        parser.error("--to and --from-number are required")

    client = SmsClient.from_connection_string(_get_connection_string())
    responses = client.send(
        from_=args.from_number,
        to=[args.to],
        message=args.message,
        enable_delivery_report=True,
        tag="clinic-recall-phase0-sms-proof",
    )

    for response in responses:
        if response.successful:
            print(f"sent to {response.to}; message_id={response.message_id}")
        else:
            print(f"failed to {response.to}: {response.error_message}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())