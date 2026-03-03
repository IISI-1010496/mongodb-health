"""
Slack 通知工具 — 從 stdin 讀取文字，POST 到 Slack Incoming Webhook

用法：
  echo "Hello" | python notify.py
  echo "Hello" | python notify.py --webhook "https://hooks.slack.com/services/..."
  python snapshot.py -q --compact | gemini -p "$(cat prompts/analyze.md)" 2>/dev/null | python notify.py

Webhook URL 來源優先順序：
  1. --webhook 參數
  2. SLACK_WEBHOOK_URL 環境變數
  3. config.json 的 slack_webhook 欄位
"""

import sys
import json
import re
import argparse
import urllib.request
import urllib.error
from pathlib import Path


def load_webhook_from_config():
    """從 config.json 讀取 slack_webhook"""
    for p in [Path("config.json"), Path(__file__).parent / "config.json"]:
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
                url = cfg.get("slack_webhook", "")
                if url:
                    return url
            except (json.JSONDecodeError, KeyError):
                pass
    return None


def resolve_webhook(args_webhook):
    """依優先順序解析 webhook URL"""
    import os

    if args_webhook:
        return args_webhook
    env = os.environ.get("SLACK_WEBHOOK_URL", "")
    if env:
        return env
    return load_webhook_from_config()


def md_to_slack(text):
    """Markdown → Slack mrkdwn 基本轉換"""
    # ### heading → *heading*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # **bold** → *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # `code` 保持不變（Slack 也用 backtick）
    # - list item 保持不變（Slack 支援）
    return text


def send_to_slack(webhook_url, text):
    """POST 文字到 Slack webhook"""
    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        return None, str(e.reason)


def main():
    parser = argparse.ArgumentParser(description="Send stdin to Slack webhook")
    parser.add_argument("--webhook", help="Slack Incoming Webhook URL")
    args = parser.parse_args()

    # 讀取 stdin
    if sys.stdin.isatty():
        print("Error: no input from stdin. Pipe something in.", file=sys.stderr)
        print("  例：echo 'Hello' | python notify.py", file=sys.stderr)
        sys.exit(1)

    text = sys.stdin.read().strip()
    if not text:
        print("Error: stdin is empty.", file=sys.stderr)
        sys.exit(1)

    # 解析 webhook URL
    webhook_url = resolve_webhook(args.webhook)
    if not webhook_url:
        print("Error: no webhook URL found.", file=sys.stderr)
        print("  用 --webhook 參數、SLACK_WEBHOOK_URL 環境變數、或 config.json 的 slack_webhook 欄位設定", file=sys.stderr)
        sys.exit(1)

    # 轉換格式並送出
    slack_text = md_to_slack(text)
    status, body = send_to_slack(webhook_url, slack_text)

    if status == 200:
        print(f"Sent to Slack ({len(slack_text)} chars)", file=sys.stderr)
    else:
        print(f"Error: Slack returned {status}: {body}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
