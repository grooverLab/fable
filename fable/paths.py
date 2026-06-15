"""fable home — every fable artifact under one directory.

Default ~/.fable/ (override the root with FABLE_HOME). Layout:

    ~/.fable/
      fable.db        the SQLite index (Map)      [FABLE_DB override]
      vault/          immutable transcript vault  [FABLE_VAULT override]
      checkpoints/    post-Bash file snapshots    [FABLE_CHECKPOINTS override]
      config.json     vault choice + legacy read-roots
      .env            provider keys

No hardcoded per-machine paths: every location resolves through env var,
then config.json, then a sane default under home().
"""
import json
import os


def home():
    return os.environ.get("FABLE_HOME") or os.path.expanduser("~/.fable")


def ensure_home():
    os.makedirs(home(), exist_ok=True)
    return home()


def config_path():
    return os.path.join(home(), "config.json")


def load_config():
    try:
        with open(config_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(updates):
    ensure_home()
    cfg = load_config()
    cfg.update(updates)
    tmp = config_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, config_path())
    return cfg


def default_db():
    return os.environ.get("FABLE_DB") or os.path.join(home(), "fable.db")


def vault_dir():
    """Where fable WRITES vault generations (prune/surgery/compaction)."""
    return (os.environ.get("FABLE_VAULT")
            or load_config().get("vault")
            or os.path.join(home(), "vault"))


def checkpoints_dir():
    return (os.environ.get("FABLE_CHECKPOINTS")
            or os.path.join(home(), "checkpoints"))


def backup_roots():
    """Directories to READ vault generations from during discover: the vault
    itself, plus opt-in legacy roots from config.json and FABLE_BACKUP_ROOTS
    (os.pathsep-separated). De-duplicated, order preserved."""
    roots = [vault_dir()]
    roots += load_config().get("backup_roots", []) or []
    extra = os.environ.get("FABLE_BACKUP_ROOTS", "")
    roots += [p for p in extra.split(os.pathsep) if p.strip()]
    seen, out = set(), []
    for r in roots:
        r = os.path.expanduser(r)
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def log_path(name):
    return os.path.join(ensure_home(), name)
