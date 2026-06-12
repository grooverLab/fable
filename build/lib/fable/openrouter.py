"""Minimal OpenRouter chat client — stdlib only.

Free-tier friendly: RPM throttle, 429/5xx backoff. The card pass uses the
free Gemma model by default; the key lives in .env (never committed).
"""
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemma-4-31b-it:free"


class OpenRouterError(Exception):
    pass


def env_path() -> str:
    """Canonical .env location: FABLE_ENV override, else <repo-root>/.env."""
    return os.environ.get("FABLE_ENV") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def save_env(updates: dict) -> str:
    """Merge KEY=VALUE updates into the .env file (0600) and the live env."""
    path = env_path()
    lines = []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.read().splitlines()
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    for key, val in updates.items():
        if key not in done:
            lines.append(f"{key}={val}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    for key, val in updates.items():
        os.environ[key] = val
    return path


def load_env(path: str = None) -> None:
    """Load KEY=VALUE lines; never override real environment variables.

    Default search order: FABLE_ENV, ./.env, then <repo-root>/.env."""
    if path is None:
        for candidate in (env_path(), ".env"):
            if candidate and os.path.exists(candidate):
                path = candidate
                break
        else:
            return
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


_last_call = [0.0]


def _throttle():
    rpm = float(os.environ.get("OPENROUTER_RPM", "15") or 15)
    min_gap = 60.0 / max(rpm, 0.001)
    wait = _last_call[0] + min_gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def chat(messages, model=None, api_key=None, base_url=None,
         max_tokens=1024, temperature=0.2, retries=3,
         retry_wait=2.0) -> str:
    """One chat completion; returns the assistant message content."""
    if api_key is None:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise OpenRouterError(
            "no API key. Set OPENROUTER_API_KEY in the environment or in "
            "fable/.env (see .env.example), then re-run.")
    model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
    base_url = base_url or os.environ.get("OPENROUTER_BASE_URL",
                                          DEFAULT_BASE_URL)

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()

    attempt = 0
    while True:
        _throttle()
        req = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/local/fable",
                "X-Title": "fable transcript recall",
            })
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode())
            try:
                return body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise OpenRouterError(f"unexpected response shape: "
                                      f"{json.dumps(body)[:500]}")
        except urllib.error.HTTPError as e:
            status = e.code
            retryable = status == 429 or status >= 500
            if retryable and attempt < retries:
                attempt += 1
                retry_after = 0.0
                try:
                    retry_after = float(e.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    pass
                time.sleep(max(retry_wait * (2 ** (attempt - 1)),
                               min(retry_after, 120)))
                continue
            raise OpenRouterError(
                f"HTTP {status} from OpenRouter after {attempt + 1} "
                f"attempt(s): {e.read().decode(errors='replace')[:300]}")
        except urllib.error.URLError as e:
            if attempt < retries:
                attempt += 1
                time.sleep(retry_wait * (2 ** (attempt - 1)))
                continue
            raise OpenRouterError(f"connection failed: {e}")
