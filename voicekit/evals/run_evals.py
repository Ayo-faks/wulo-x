"""voicekit eval gate: paired reference agents + determinism check.

For every corpus case the BAD agent must be caught (a detector or the strict
fake raises) and the GOOD agent must pass cleanly. Any false negative or false
positive fails the gate. The whole suite is then repeated with shuffled order
to prove determinism. Exit code 0 = ship gate green.

Usage: python evals/run_evals.py [--repeats N]
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cases import CASES, Case  # noqa: E402


async def _caught(agent) -> tuple[bool, str]:
    """Run a reference agent; return (raised, message)."""
    try:
        await agent()
    except (AssertionError, TypeError) as exc:
        return True, f"{type(exc).__name__}: {exc}"
    return False, ""


async def run_case(case: Case) -> list[str]:
    problems: list[str] = []
    bad_caught, bad_msg = await _caught(case.bad)
    good_caught, good_msg = await _caught(case.good)
    if not bad_caught:
        problems.append(f"{case.id} FALSE NEGATIVE: bad agent was not caught ({case.failure_mode})")
    if good_caught:
        problems.append(f"{case.id} FALSE POSITIVE: good agent was flagged — {good_msg}")
    if bad_caught and not good_caught:
        first_line = bad_msg.splitlines()[0]
        print(f"  ok {case.id}  {case.failure_mode}")
        print(f"       bad agent caught: {first_line[:110]}")
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5, help="determinism repeats with shuffled order")
    args = parser.parse_args()

    all_problems: list[str] = []
    print(f"voicekit eval gate — {len(CASES)} corpus cases, {args.repeats} shuffled repeats\n")
    for repeat in range(args.repeats):
        order = list(CASES)
        random.Random(repeat).shuffle(order)
        if repeat == 0:
            for case in order:
                all_problems.extend(await run_case(case))
        else:
            for case in order:
                for problem in await run_case_quiet(case):
                    all_problems.append(f"repeat {repeat}: {problem}")

    print()
    if all_problems:
        print("EVAL GATE RED:")
        for problem in all_problems:
            print(f"  ✗ {problem}")
        return 1
    print(f"EVAL GATE GREEN: {len(CASES)}/{len(CASES)} caught, 0 false positives, "
          f"deterministic across {args.repeats} shuffled runs")
    return 0


async def run_case_quiet(case: Case) -> list[str]:
    problems: list[str] = []
    bad_caught, _ = await _caught(case.bad)
    good_caught, good_msg = await _caught(case.good)
    if not bad_caught:
        problems.append(f"{case.id} FALSE NEGATIVE")
    if good_caught:
        problems.append(f"{case.id} FALSE POSITIVE — {good_msg}")
    return problems


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
