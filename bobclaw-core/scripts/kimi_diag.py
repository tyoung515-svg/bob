"""One-shot Kimi diagnostic — both kimi_code (KIMI_*) and kimi_platform
(KIMI_PLATFORM_* / MOONSHOT_API_KEY) — to figure out where your key lives.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backends.kimi import KimiClient
from core.backends.kimi_platform import KimiPlatformClient
from core.config import config


async def main() -> None:
    print("=== kimi_code  (uses KIMI_API_KEY / KIMI_BASE_URL / KIMI_MODEL) ===")
    print(f"  KIMI_BASE_URL: {config.KIMI_BASE_URL}")
    print(f"  KIMI_MODEL:    {config.KIMI_MODEL}")
    print(f"  KIMI_API_KEY:  {'set (' + str(len(config.KIMI_API_KEY)) + ' chars)' if config.KIMI_API_KEY else 'unset'}")
    print()
    print("=== kimi_platform  (uses MOONSHOT_API_KEY / KIMI_PLATFORM_BASE_URL / KIMI_PLATFORM_MODEL) ===")
    print(f"  KIMI_PLATFORM_BASE_URL: {config.KIMI_PLATFORM_BASE_URL}")
    print(f"  KIMI_PLATFORM_MODEL:    {config.KIMI_PLATFORM_MODEL}")
    print(f"  MOONSHOT_API_KEY:       {'set (' + str(len(config.MOONSHOT_API_KEY)) + ' chars)' if config.MOONSHOT_API_KEY else 'unset'}")
    print()

    c = KimiClient()
    print("health_check (hits /v1/models)...")
    try:
        h = await c.health_check()
        print(f"  -> {h}")
    except Exception as e:
        print(f"  -> raised: {type(e).__name__}: {e}")

    print("\nlist_models (also /v1/models)...")
    try:
        m = await c.list_models()
        print(f"  -> {m}")
    except Exception as e:
        print(f"  -> raised: {type(e).__name__}: {e}")

    print("\nchat completion (POST /chat/completions, raw)...")
    import aiohttp
    payload = {
        "model": config.KIMI_MODEL,
        "messages": [{"role": "user", "content": "Reply with the single word OK."}],
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {config.KIMI_API_KEY}"}
    url = f"{config.KIMI_BASE_URL.rstrip('/')}/chat/completions"
    print(f"  POST {url}")
    print(f"  payload: {payload}")
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers) as resp:
            body = await resp.text()
            print(f"  status: {resp.status}")
            print(f"  response headers (selected):")
            for k in ("content-type", "x-request-id", "x-ratelimit-remaining", "retry-after", "www-authenticate"):
                if k in resp.headers:
                    print(f"    {k}: {resp.headers[k]}")
            print(f"  body: {body[:600]}")

    if config.MOONSHOT_API_KEY:
        print("\n--- kimi_platform chat completion (POST /chat/completions, raw) ---")
        kp_url = f"{config.KIMI_PLATFORM_BASE_URL.rstrip('/')}/chat/completions"
        kp_payload = {
            "model": config.KIMI_PLATFORM_MODEL,
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "stream": False,
        }
        kp_headers = {"Authorization": f"Bearer {config.MOONSHOT_API_KEY}"}
        print(f"  POST {kp_url}")
        print(f"  payload: {kp_payload}")
        async with aiohttp.ClientSession() as s:
            async with s.post(kp_url, json=kp_payload, headers=kp_headers) as resp:
                kp_body = await resp.text()
                print(f"  status: {resp.status}")
                print(f"  body: {kp_body[:600]}")
    else:
        print("\n(skipping kimi_platform chat — MOONSHOT_API_KEY not set)")


if __name__ == "__main__":
    asyncio.run(main())
