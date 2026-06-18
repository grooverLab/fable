"""fable setup — one-time onboarding: pick the ~/.fable home + vault location,
optionally migrate an existing index db and register legacy backup folders.

Everything fable owns lives under one directory (default ~/.fable):
    fable.db · vault/ · checkpoints/ · config.json · .env

Out of the box this needs no input (sensible defaults). The dashboard's
first-run and `fable setup` both write the same ~/.fable/config.json.
"""
import json
import os
import re
import shutil

from fable import paths


def _settings_path():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(cfg, "settings.json")


def repair_hooks() -> dict:
    """Strip stale `--db <path>` flags from fable's Claude Code hook commands
    so they fall back to the resolved default db (~/.fable/fable.db).

    A db migration moves the file but can't touch settings.json; without this,
    every hook keeps indexing into a recreated stub at the old path. Idempotent
    and best-effort — never raises if settings.json is absent or unparseable."""
    path = _settings_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"settings": path, "repaired": 0, "skipped": "no settings.json"}
    fixed = 0
    for groups in (data.get("hooks") or {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if "fable" in cmd and "--db" in cmd:
                    new = re.sub(r"\s+--db\s+\S+", "", cmd)
                    if new != cmd:
                        h["command"] = new
                        fixed += 1
    if fixed:
        shutil.copy2(path, path + ".bak-fable")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    return {"settings": path, "repaired": fixed}


def run_setup(vault=None, legacy_roots=None, migrate_db=None,
              move_env=None) -> dict:
    home = paths.ensure_home()
    vault = os.path.abspath(os.path.expanduser(vault)) if vault else paths.vault_dir()
    os.makedirs(vault, exist_ok=True)
    os.makedirs(paths.checkpoints_dir(), exist_ok=True)

    cfg = {"vault": vault}
    if legacy_roots:
        cfg["backup_roots"] = [os.path.abspath(os.path.expanduser(r))
                               for r in legacy_roots
                               if os.path.isdir(os.path.expanduser(r))]

    moved_db = None
    if migrate_db:
        src = os.path.abspath(os.path.expanduser(migrate_db))
        dest = paths.default_db()
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dest):
            # move the db plus its WAL/SHM sidecars together
            for ext in ("", "-wal", "-shm"):
                if os.path.exists(src + ext):
                    shutil.move(src + ext, dest + ext)
            moved_db = dest

    moved_env = None
    if move_env:
        src = os.path.abspath(os.path.expanduser(move_env))
        dest = os.path.join(home, ".env")
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dest):
            shutil.copy2(src, dest)
            os.chmod(dest, 0o600)
            moved_env = dest

    paths.save_config(cfg)
    # if we relocated the db, the Claude Code hooks may still pin the old path
    hooks = repair_hooks() if moved_db else {"repaired": 0}
    return {"home": home, "vault": vault, "db": paths.default_db(),
            "checkpoints": paths.checkpoints_dir(),
            "config": paths.config_path(),
            "backup_roots": paths.backup_roots(),
            "migrated_db": moved_db, "migrated_env": moved_env,
            "hooks_repaired": hooks["repaired"]}


def cmd_setup(args) -> int:
    info = run_setup(vault=args.vault, legacy_roots=args.legacy_roots,
                     migrate_db=args.migrate_db, move_env=args.move_env)
    print(json.dumps(info, indent=2))
    return 0


# fable's Claude Code hooks: event -> per-event options (matcher / timeout).
# Same set the dashboard reports as "hook_installed".
HOOK_EVENTS = {
    "SessionStart": {"timeout": 30},
    "UserPromptSubmit": {},
    "Stop": {},
    "SubagentStop": {},
    "PreCompact": {"timeout": 120},
    "PreToolUse": {"matcher": "Edit|Write|MultiEdit", "timeout": 20},
    "PostToolUse": {"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|Read"
                               "|TaskCreate|TaskUpdate"},
}


def install_hooks(command="fable hook") -> dict:
    """Idempotently register fable's hooks in Claude Code settings.json.
    Skips any event that already has a fable hook. Best-effort, never raises."""
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    hooks = data.setdefault("hooks", {})
    installed = 0
    for event, opts in HOOK_EVENTS.items():
        groups = hooks.setdefault(event, [])
        if any("fable" in hk.get("command", "")
               for g in groups for hk in g.get("hooks", [])):
            continue  # already wired
        entry = {"type": "command", "command": command}
        if opts.get("timeout"):
            entry["timeout"] = opts["timeout"]
        group = {"hooks": [entry]}
        if opts.get("matcher"):
            group["matcher"] = opts["matcher"]
        groups.append(group)
        installed += 1
    if installed:
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak-fable")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    return {"installed": installed, "settings": path}


def register_mcp() -> str:
    """Register the fable MCP server with Claude Code (user scope). Best-effort."""
    import subprocess
    try:
        r = subprocess.run(
            ["claude", "mcp", "add", "--scope", "user", "fable", "--",
             "fable", "mcp"],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return "registered (user scope)"
        msg = ((r.stderr or "") + (r.stdout or "")).strip().lower()
        if "already" in msg:
            return "already registered"
        return "run manually: claude mcp add fable -- fable mcp"
    except FileNotFoundError:
        return "claude CLI not found — run: claude mcp add fable -- fable mcp"
    except Exception as e:  # pragma: no cover
        return "skipped (%s)" % str(e)[:60]


def cmd_install(args) -> int:
    """One-command onboarding: ~/.fable home + MCP registration + Claude Code
    hooks + an initial transcript scan."""
    import subprocess
    import sys
    print("⊹ fable install")
    info = run_setup()
    print("  home    ", info["home"])
    print("  vault   ", info["vault"])
    print("  db      ", info["db"])
    print("  mcp     ", register_mcp())
    hk = install_hooks(command=args.hook_command)
    print("  hooks   ", "%d newly wired -> %s" % (hk["installed"], hk["settings"]))
    if not args.no_index:
        print("  index    scanning Claude Code transcripts "
              "(first run can take a minute)…")
        try:
            subprocess.run([sys.executable, "-m", "fable", "discover"],
                           check=False)
        except Exception as e:
            print("  index    skipped (%s) — run `fable discover` later"
                  % str(e)[:80])
    print("\n✅ fable is set up. Launch the dashboard:  fable serve")
    return 0
