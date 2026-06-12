"""LLM provider abstraction for the card pass.

Three backends, user-selected:
  openrouter — free Gemma via OpenRouter (OPENROUTER_API_KEY)
  anthropic  — Anthropic API (ANTHROPIC_API_KEY), haiku/sonnet
  claude-cli — headless `claude -p` through the installed Claude Code,
               which bills the user's Max/Pro SUBSCRIPTION quota. This is
               the legitimate way to leverage a subscription: the OAuth
               token never leaves Claude Code, fable just shells out.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from fable.openrouter import chat as openrouter_chat, load_env


class ProviderError(RuntimeError):
    pass


PROVIDERS = ("openrouter", "anthropic", "claude-cli", "ollama")

ANTHROPIC_MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}


def complete(prompt: str, provider: str = "openrouter", model=None,
             **kw) -> str:
    load_env()
    if provider == "openrouter":
        return openrouter_chat([{"role": "user", "content": prompt}],
                               model=model, **kw)
    if provider == "anthropic":
        return _anthropic(prompt, model or "haiku", **kw)
    if provider == "claude-cli":
        return _claude_cli(prompt, model or "haiku", **kw)
    if provider == "ollama":
        return _ollama(prompt, model, **kw)
    raise ProviderError(f"unknown provider {provider!r} "
                        f"(choose from {PROVIDERS})")


def _ollama(prompt, model=None, max_tokens=1024, retries=2, retry_wait=2.0,
            base_url=None, timeout=300, **_):
    """Local models via Ollama — including user-trained/fine-tuned ones.

    Any model `ollama list` shows works: --provider ollama --model qwen2.5
    No key, no network, fully private."""
    model = model or os.environ.get("OLLAMA_MODEL", "llama3.2")
    base = (base_url or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:11434")
    payload = json.dumps({
        "model": model, "stream": False,
        "options": {"num_predict": max_tokens},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    attempt = 0
    while True:
        req = urllib.request.Request(
            base.rstrip("/") + "/api/chat", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
            content = (body.get("message") or {}).get("content", "")
            if not content:
                raise ProviderError(
                    f"ollama returned empty content: "
                    f"{json.dumps(body)[:200]}")
            return content
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code == 404 and "model" in detail.lower():
                raise ProviderError(
                    f"ollama model {model!r} not found — "
                    f"run: ollama pull {model}")
            if e.code >= 500 and attempt < retries:
                attempt += 1
                time.sleep(retry_wait * (2 ** (attempt - 1)))
                continue
            raise ProviderError(f"ollama HTTP {e.code}: {detail}")
        except urllib.error.URLError:
            raise ProviderError(
                "ollama is not reachable at " + base +
                " — start it with: ollama serve (or brew services "
                "start ollama)")


def _anthropic(prompt, model, max_tokens=1024, retries=3, retry_wait=2.0,
               base_url=None, api_key=None, **_):
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ProviderError(
            "no ANTHROPIC_API_KEY — add it in the dashboard Settings panel "
            "or in fable/.env")
    model = ANTHROPIC_MODELS.get(model, model)
    base = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com")
    payload = json.dumps({
        "model": model, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    attempt = 0
    while True:
        req = urllib.request.Request(
            base.rstrip("/") + "/v1/messages", data=payload,
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
            return "".join(b.get("text", "") for b in body.get("content", [])
                           if b.get("type") == "text")
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                attempt += 1
                time.sleep(retry_wait * (2 ** (attempt - 1)))
                continue
            raise ProviderError(
                f"anthropic HTTP {e.code}: "
                f"{e.read().decode(errors='replace')[:200]}")
        except urllib.error.URLError as e:
            if attempt < retries:
                attempt += 1
                time.sleep(retry_wait * (2 ** (attempt - 1)))
                continue
            raise ProviderError(f"anthropic connection failed: {e}")


def _claude_cli(prompt, model, timeout=300, claude_bin=None, **_):
    binary = claude_bin or os.environ.get("CLAUDE_BIN", "claude")
    # CRITICAL anti-inception measure: headless card-generation sessions
    # must never land in ~/.claude/projects, or fable would re-index its
    # own card prompts (which contain rendered thread text). A scratch
    # CLAUDE_CONFIG_DIR keeps them out of the real projects tree entirely
    # (macOS auth lives in the Keychain, so login survives).
    env = dict(os.environ)
    scratch = os.environ.get("FABLE_CLAUDE_SCRATCH") or os.path.join(
        os.path.expanduser("~/.fable"), "claude-scratch")
    os.makedirs(scratch, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = scratch
    def _run(use_env):
        return subprocess.run(
            [binary, "-p", "--model", model],
            input=prompt, capture_output=True, text=True, timeout=timeout,
            env=use_env)
    try:
        proc = _run(env)
        if "Not logged in" in (proc.stdout + proc.stderr):
            # scratch config isn't authenticated on this machine — fall back
            # to the real config; the FABLE-GENERATED prompt marker keeps
            # these sessions out of the index (anti-inception layer 2)
            proc = _run(None)
    except FileNotFoundError:
        raise ProviderError(
            "claude CLI not found — install Claude Code or set CLAUDE_BIN")
    except subprocess.TimeoutExpired:
        raise ProviderError(f"claude CLI timed out after {timeout}s")
    if proc.returncode != 0 or "Not logged in" in proc.stdout:
        raise ProviderError(
            f"claude CLI exit {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[:200]}")
    return proc.stdout


def availability() -> dict:
    load_env()
    return {
        "openrouter": bool(os.environ.get("OPENROUTER_API_KEY")),
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "claude-cli": shutil.which(
            os.environ.get("CLAUDE_BIN", "claude")) is not None,
    }
