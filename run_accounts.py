#!/usr/bin/env python3
"""
Launches one subprocess per Telegram account (fully isolated — separate
process, separate session file, separate global state) and merges all their
output into a single live terminal stream, tagged by account, so you only
need to watch one screen instead of juggling multiple terminal tabs.

Each account keeps running independently: if one crashes it doesn't take the
other down, and each has its own session/globals so wizard puzzles, battle
state, etc. from one account can never leak into the other's.

Usage:
    python3 run_accounts.py                     # uses accounts_config.json
    python3 run_accounts.py my_other_config.json # custom config path

Config format (accounts_config.json): a JSON list of account objects.
Required keys per account: "tag", "script", "TG_API_ID", "TG_API_HASH",
"TG_PHONE", "TG_SESSION_NAME". Any other TG_* key gets passed through as an
env var too (e.g. "TG_BOT_USERNAME" if you ever need to point at a
different game bot per account).
"""
import asyncio
import json
import os
import sys

CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else "accounts_config.json"


async def stream_output(proc, tag):
    """Read the subprocess's stdout line by line and print it immediately,
    so output from all accounts interleaves live instead of appearing in
    big delayed chunks."""
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        # Each script's own log() already prefixes with [tag], but stray
        # prints (tracebacks, startup banners) won't have it — add a
        # fallback prefix so every line is still attributable.
        text = line.decode(errors="replace").rstrip("\n")
        if text.startswith(f"[{tag}]"):
            print(text)
        else:
            print(f"[{tag}] {text}")


async def run_account(cfg):
    tag = cfg["tag"]
    script = cfg.get("script", "ordinal_cap.py")

    env = os.environ.copy()
    env["ACCOUNT_TAG"] = tag
    for key, value in cfg.items():
        if key.startswith("TG_"):
            env[key] = str(value)

    while True:
        print(f"[{tag}] Starting {script}...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await stream_output(proc, tag)
        code = await proc.wait()
        print(f"[{tag}] {script} exited (code={code}). Restarting in 5s...")
        await asyncio.sleep(5)


async def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Config file not found: {CONFIG_PATH}")
        print("Copy/edit accounts_config.json first — see the header comment "
              "in run_accounts.py for the format.")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        accounts = json.load(f)

    if not accounts:
        print("No accounts defined in config.")
        sys.exit(1)

    print(f"Launching {len(accounts)} account(s): {[a['tag'] for a in accounts]}")
    await asyncio.gather(*(run_account(cfg) for cfg in accounts))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
