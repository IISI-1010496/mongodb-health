"""
Gemini AI 分析工具 — 從 stdin 讀取 snapshot JSON，呼叫 Gemini API 產生分析報告

用法：
  python snapshot.py -q --compact | python analyze.py
  python snapshot.py -q --compact | python analyze.py --api-key "AIza..."
  python snapshot.py -q --compact | python analyze.py --model gemini-2.5-flash

API Key 來源優先順序：
  1. --api-key 參數
  2. GEMINI_API_KEY 環境變數
  3. config.json 的 gemini_api_key 欄位
"""

import sys
import json
import argparse
import urllib.request
import urllib.error
from pathlib import Path

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash"


def load_api_key_from_config():
    for p in [Path("config.json"), Path(__file__).parent / "config.json"]:
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
                key = cfg.get("gemini_api_key", "")
                if key:
                    return key
            except (json.JSONDecodeError, KeyError):
                pass
    return None


def resolve_api_key(args_key):
    import os

    if args_key:
        return args_key
    env = os.environ.get("GEMINI_API_KEY", "")
    if env:
        return env
    return load_api_key_from_config()


def load_prompt():
    for p in [Path("prompts/analyze.md"), Path(__file__).parent / "prompts" / "analyze.md"]:
        if p.exists():
            return p.read_text(encoding="utf-8")
    print("Error: prompts/analyze.md not found.", file=sys.stderr)
    sys.exit(1)


def call_gemini(api_key, model, prompt, data):
    url = API_URL.format(model=model) + f"?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": f"{prompt}\n\n{data}"}]}],
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            return result["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"Error: Gemini API returned {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, IndexError):
        print("Error: unexpected Gemini API response format.", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Analyze MongoDB snapshot with Gemini AI")
    parser.add_argument("--api-key", help="Gemini API key")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    if sys.stdin.isatty():
        print("Error: no input from stdin. Pipe snapshot.py output in.", file=sys.stderr)
        print("  例：python snapshot.py -q --compact | python analyze.py", file=sys.stderr)
        sys.exit(1)

    data = sys.stdin.read().strip()
    if not data:
        print("Error: stdin is empty.", file=sys.stderr)
        sys.exit(1)

    api_key = resolve_api_key(args.api_key)
    if not api_key:
        print("Error: no Gemini API key found.", file=sys.stderr)
        print("  用 --api-key 參數、GEMINI_API_KEY 環境變數、或 config.json 的 gemini_api_key 欄位設定", file=sys.stderr)
        sys.exit(1)

    prompt = load_prompt()
    print(f"Analyzing with {args.model}...", file=sys.stderr)
    result = call_gemini(api_key, args.model, prompt, data)
    print(result)


if __name__ == "__main__":
    main()
