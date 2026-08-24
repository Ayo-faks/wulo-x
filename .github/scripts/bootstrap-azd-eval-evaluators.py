#!/usr/bin/env python3
"""Seed azd custom evaluators before `azd ai agent eval update` runs.

The azd update command can create a new version only after the evaluator name
already exists in the Foundry project. This script creates the initial rubric
definition from local `rubric_dimensions.json` files when needed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml


DEFAULT_API_VERSION = "2025-11-15-preview"


def resolve_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def get_project_endpoint() -> str:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get(
        "AZURE_AI_FOUNDRY_PROJECT_ENDPOINT"
    )
    if endpoint:
        return endpoint.rstrip("/")

    try:
        endpoint = subprocess.check_output(
            ["azd", "env", "get-value", "FOUNDRY_PROJECT_ENDPOINT"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        endpoint = ""

    if not endpoint:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT or AZURE_AI_FOUNDRY_PROJECT_ENDPOINT is required"
        )
    return endpoint.rstrip("/")


def get_ai_token() -> str:
    return subprocess.check_output(
        [
            "az",
            "account",
            "get-access-token",
            "--resource",
            "https://ai.azure.com",
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        text=True,
    ).strip()


def request_json(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310 - trusted Azure URL
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"request failed: {exc.reason}") from exc


def evaluator_exists(endpoint: str, name: str, version: str, token: str, api_version: str) -> bool:
    encoded_name = quote(name, safe="")
    encoded_version = quote(version, safe="")
    url = (
        f"{endpoint}/evaluators/{encoded_name}/versions/{encoded_version}"
        f"?api-version={api_version}"
    )
    try:
        request_json("GET", url, token)
        return True
    except RuntimeError as exc:
        if str(exc).startswith("HTTP 404:"):
            return False
        raise


def create_evaluator(
    endpoint: str,
    name: str,
    dimensions_path: Path,
    token: str,
    api_version: str,
) -> str:
    dimensions = json.loads(dimensions_path.read_text(encoding="utf-8"))
    for dimension in dimensions:
        dimension.setdefault("always_applicable", False)

    body = {
        "name": name,
        "display_name": name,
        "description": "",
        "categories": ["quality", "agents"],
        "supported_evaluation_levels": ["turn", "conversation"],
        "evaluator_type": "custom",
        "definition": {
            "type": "rubric",
            "dimensions": dimensions,
            "pass_threshold": 0.5,
            "init_parameters": {
                "required": ["model"],
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Model deployment name for the LLM judge",
                    }
                },
            },
            "metrics": {
                name: {
                    "type": "continuous",
                    "desirable_direction": "increase",
                    "min_value": 0.0,
                    "max_value": 1.0,
                    "is_primary": True,
                }
            },
            "data_schema": {
                "required": [],
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "User query (turn mode)"},
                    "response": {
                        "type": "string",
                        "description": "Agent response (turn mode)",
                    },
                    "messages": {
                        "type": "array",
                        "description": "Conversation messages (conversation mode)",
                    },
                    "tool_definitions": {
                        "type": "array",
                        "description": "Tool definitions available to the agent",
                    },
                },
            },
            "prompt_text": "",
        },
    }

    encoded_name = quote(name, safe="")
    url = f"{endpoint}/evaluators/{encoded_name}/versions?api-version={api_version}"
    response = request_json("POST", url, token, body)
    return str(response.get("version") or "1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to azd eval YAML")
    parser.add_argument(
        "--api-version",
        default=os.environ.get("AGENTOPS_FOUNDRY_EVAL_API_VERSION", DEFAULT_API_VERSION),
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    evaluators = config.get("evaluators") or []
    local_evaluators = [
        evaluator
        for evaluator in evaluators
        if isinstance(evaluator, dict)
        and evaluator.get("name")
        and evaluator.get("local_uri")
        and not str(evaluator["name"]).startswith("builtin.")
    ]
    if not local_evaluators:
        print("No local custom evaluators to bootstrap.")
        return 0

    endpoint = get_project_endpoint()
    token = get_ai_token()
    changed = False
    base_dir = config_path.parent

    for evaluator in local_evaluators:
        name = str(evaluator["name"])
        version = str(evaluator.get("version") or "1")
        if evaluator.get("version") != version:
            evaluator["version"] = version
            changed = True

        dimensions_path = resolve_path(str(evaluator["local_uri"]), base_dir)
        if not dimensions_path.exists():
            raise FileNotFoundError(f"{name}: local_uri not found: {dimensions_path}")

        if evaluator_exists(endpoint, name, version, token, args.api_version):
            print(f"Evaluator {name} version {version} exists.")
            continue

        created_version = create_evaluator(endpoint, name, dimensions_path, token, args.api_version)
        print(f"Created evaluator {name} version {created_version}.")
        if version != created_version:
            evaluator["version"] = created_version
            changed = True

    if changed:
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())