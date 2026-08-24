#!/usr/bin/env python3
"""Fetch per-row Foundry hosted-eval results (instance_results.jsonl).

Downloads eval run output items from the Foundry project evals API so hosted
smoke failures can be root-caused row-by-row instead of from aggregate scores.

Auth: requires `az login` (azd auth is NOT sufficient for the evals API).

Usage:
  python3 devops/agentops/fetch_foundry_instance_results.py                # latest run
  python3 devops/agentops/fetch_foundry_instance_results.py --results-dir .agentops/results/2026-07-01T14-10-43Z
  python3 devops/agentops/fetch_foundry_instance_results.py --eval-id eval_x --run-id evalrun_y

Output:
  <results-dir>/instance_results.jsonl   raw output items, one JSON object per row
  <results-dir>/instance_results.md      failed-row summary table (also printed)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_DIR = REPO_ROOT / ".agentops" / "results" / "latest"
TOKEN_SCOPE = "https://ai.azure.com/.default"
PAGE_LIMIT = 100


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def get_token() -> str:
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--scope", TOKEN_SCOPE,
             "--query", "accessToken", "-o", "tsv"],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        fail("az CLI not found. Install it and run `az login`.")
    except subprocess.CalledProcessError as exc:
        fail(f"az token acquisition failed (run `az login`): {exc.stderr.strip()}")
    return out.stdout.strip()


def resolve_endpoint(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")
    # Prefer the default azd env file; fall back to any env dir.
    azure_dir = REPO_ROOT / ".azure"
    candidates = sorted(azure_dir.glob("*/.env")) if azure_dir.is_dir() else []
    for env_file in candidates:
        for line in env_file.read_text().splitlines():
            if line.startswith("FOUNDRY_PROJECT_ENDPOINT="):
                return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    fail("FOUNDRY_PROJECT_ENDPOINT not found; pass --endpoint or set up an azd env.")
    raise AssertionError  # unreachable


def resolve_run(results_dir: Path) -> tuple[str, str, dict]:
    results_json = results_dir / "results.json"
    if not results_json.is_file():
        fail(f"{results_json} not found; pass --eval-id/--run-id explicitly.")
    data = json.loads(results_json.read_text())
    azd_eval = (data.get("config") or {}).get("azd_evaluation") or {}
    eval_id, run_id = azd_eval.get("eval_id"), azd_eval.get("run_id")
    if not (eval_id and run_id):
        fail(f"no config.azd_evaluation.eval_id/run_id in {results_json}")
    return eval_id, run_id, data


def fetch_output_items(endpoint: str, eval_id: str, run_id: str, token: str) -> list[dict]:
    base = f"{endpoint}/openai/v1/evals/{eval_id}/runs/{run_id}/output_items"
    items: list[dict] = []
    after: str | None = None
    while True:
        params = {"limit": str(PAGE_LIMIT)}
        if after:
            params["after"] = after
        url = f"{base}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                page = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            fail(f"evals API {exc.code} for {url}: {exc.read().decode(errors='replace')[:500]}")
        data = page.get("data") or []
        items.extend(data)
        if page.get("has_more") and data:
            after = data[-1].get("id")
        else:
            return items


def row_title(item: dict) -> str:
    src = item.get("datasource_item") or {}
    query = src.get("query") or src.get("input") or ""
    return " ".join(str(query).split())[:90]


def summarize(items: list[dict]) -> tuple[str, int]:
    lines = [
        "| row | agent | grader | score | passed | status | error |",
        "|---|---|---|---|---|---|---|",
    ]
    failed_rows = 0
    for item in items:
        src = item.get("datasource_item") or {}
        agent = f"{src.get('agent_name')}:{src.get('agent_version')}"
        row_id = item.get("datasource_item_id")
        row_failed = False
        for res in item.get("results") or []:
            passed = res.get("passed")
            status = res.get("status")
            error = ""
            sample = res.get("sample") or {}
            if isinstance(sample, dict) and sample.get("error"):
                error = str(sample["error"].get("message", ""))[:120]
            if passed is False or status == "error":
                row_failed = True
            lines.append(
                f"| {row_id}: {row_title(item)} | {agent} | {res.get('name')} "
                f"| {res.get('score')} | {passed} | {status} | {error} |"
            )
        if row_failed:
            failed_rows += 1
    return "\n".join(lines), failed_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR,
                        help="agentops results dir containing results.json (default: latest)")
    parser.add_argument("--eval-id", help="explicit Foundry eval id (overrides results.json)")
    parser.add_argument("--run-id", help="explicit Foundry eval run id (overrides results.json)")
    parser.add_argument("--endpoint", help="Foundry project endpoint (default: azd env FOUNDRY_PROJECT_ENDPOINT)")
    parser.add_argument("--out", type=Path, help="output JSONL path (default: <results-dir>/instance_results.jsonl)")
    args = parser.parse_args()

    endpoint = resolve_endpoint(args.endpoint)
    if args.eval_id and args.run_id:
        eval_id, run_id = args.eval_id, args.run_id
    elif args.eval_id or args.run_id:
        fail("--eval-id and --run-id must be provided together")
    else:
        eval_id, run_id, _ = resolve_run(args.results_dir)

    print(f"==> fetching output items for {eval_id} / {run_id}")
    token = get_token()
    items = fetch_output_items(endpoint, eval_id, run_id, token)
    if not items:
        fail("no output items returned; check the run id or Foundry retention")

    out_path = args.out or (args.results_dir / "instance_results.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    table, failed_rows = summarize(items)
    md_path = out_path.with_suffix(".md")
    md_path.write_text(
        f"# Instance results — {eval_id} / {run_id}\n\n"
        f"Rows: {len(items)}, rows with failures/errors: {failed_rows}\n\n{table}\n"
    )
    print(f"==> wrote {out_path} ({len(items)} rows) and {md_path}")
    print(f"==> rows with failed/errored graders: {failed_rows}/{len(items)}")
    print(table)


if __name__ == "__main__":
    main()
