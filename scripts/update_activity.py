#!/usr/bin/env python3
"""Update the public profile with aggregate local activity only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
START = "<!-- activity:start -->"
END = "<!-- activity:end -->"


def codex_runtime_hours() -> int:
    db = Path.home() / ".codex" / "thread_history_1.sqlite"
    if not db.exists():
        raise FileNotFoundError(f"Codex history not found: {db}")
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT started_at, completed_at, duration_ms FROM thread_turns "
            "WHERE status IN ('completed', 'interrupted', 'failed') "
            "AND completed_at IS NOT NULL AND duration_ms > 0"
        )
        intervals = []
        for started, completed, duration_ms in rows:
            beginning = started if started is not None else completed - duration_ms / 1000
            if 0 < beginning <= completed:
                intervals.append((beginning, completed))
    finally:
        con.close()

    intervals.sort()
    merged: list[tuple[float, float]] = []
    for beginning, end in intervals:
        if merged and beginning <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((beginning, end))
    return round(sum(end - beginning for beginning, end in merged) / 3600)


def minecraft_hours() -> int:
    instances = Path.home() / ".local/share/PrismLauncher/instances"
    if not instances.is_dir():
        raise FileNotFoundError(f"Prism instances not found: {instances}")
    seconds = 0
    for config in instances.glob("*/instance.cfg"):
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("totalTimePlayed="):
                seconds += int(line.partition("=")[2])
                break
    return round(seconds / 3600)


def account_usage() -> tuple[int, int]:
    codex = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    process = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        messages = [
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "github_profile_activity",
                        "title": "GitHub Profile Activity",
                        "version": "0.1.0",
                    }
                },
            },
            {"method": "initialized", "params": {}},
            {"method": "account/usage/read", "id": 2},
        ]
        for message in messages:
            process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

        deadline = time.monotonic() + 15
        buffer = ""
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [process.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                reply = json.loads(line)
                if reply.get("id") != 2:
                    continue
                if "error" in reply:
                    error = reply["error"]
                    message = str(error.get("message", error) if isinstance(error, dict) else error)
                    raise RuntimeError(message.split("; body=", 1)[0][:240])
                summary = reply["result"]["summary"]
                tokens = summary.get("lifetimeTokens")
                streak = summary.get("currentStreakDays")
                if tokens is None or streak is None:
                    raise RuntimeError("ChatGPT activity is incomplete")
                return int(tokens), int(streak)
        raise TimeoutError("ChatGPT activity request timed out")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        return f"{tokens / 1_000_000_000:.2f}B"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    return f"{tokens:,}"


def update_readme() -> bool:
    original = README.read_text(encoding="utf-8")
    codex = codex_runtime_hours()
    minecraft = minecraft_hours()
    try:
        tokens, streak = account_usage()
        chatgpt_line = (
            f"- **ChatGPT profile:** {format_tokens(tokens)} tokens · "
            f"{streak}-day streak"
        )
    except (OSError, RuntimeError, TimeoutError, KeyError, ValueError) as exc:
        previous = re.search(r"^- \*\*ChatGPT (?:profile|\+ Codex):\*\* .+$", original, re.M)
        if previous is None:
            raise
        chatgpt_line = previous.group(0).replace("**ChatGPT + Codex:**", "**ChatGPT profile:**")
        print(f"ChatGPT activity unavailable; keeping previous value: {exc}")
    today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    block = (
        f"{START}\n"
        "### Activity\n\n"
        f"- **Codex:** {codex:,} h of recorded agent runtime since April 2026\n"
        f"{chatgpt_line}\n"
        f"- **Minecraft:** {minecraft:,} h in Prism Launcher\n\n"
        "<sub>Local Codex task history and Prism Launcher playtime; "
        f"ChatGPT account activity. Local counters updated {today} while my PC is on.</sub>\n"
        f"{END}"
    )
    if START in original and END in original:
        before, rest = original.split(START, 1)
        _, after = rest.split(END, 1)
        revised = before + block + after
    else:
        anchor = "`FOSS first` · `own your tools` · `make weird things`"
        if anchor not in original:
            raise ValueError("README insertion point not found")
        revised = original.replace(anchor, block + "\n\n" + anchor, 1)
    if revised == original:
        return False
    README.write_text(revised, encoding="utf-8")
    return True


def git(*args: str) -> None:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    subprocess.run(["git", "-C", str(ROOT), *args], check=True, env=env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true", help="pull, commit and push")
    args = parser.parse_args()
    if args.sync:
        git("pull", "--ff-only", "origin", "main")
    changed = update_readme()
    if args.sync and changed:
        git("add", "README.md")
        git("commit", "-m", "Update profile activity")
        git("push", "origin", "main")
    print("updated" if changed else "already current")


if __name__ == "__main__":
    main()
