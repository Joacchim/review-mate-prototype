"""GitLab connection config — resolve credentials portably (env first, then glab), build providers.

Self-contained by default (D11): reads `REVIEW_MATE_GITLAB_*` / `GITLAB_*` env, then falls back to
the local `glab` CLI's config for ambient credentials (D8). No token configured → no provider.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx

from review_mate.host.base import GITLAB_CAPABILITIES, parse_reference
from review_mate.host.gitlab import GitLabProvider, GitLabWriter


class GitLabConfig:
    def __init__(self, base_url: str, token: str, username: str, host: str,
                 git_protocol: str = "https"):
        self.base_url = base_url          # …/api/v4
        self.token = token
        self.username = username
        self.host = host
        self.git_protocol = git_protocol  # ssh|https — the git access method chosen in glab


def _glab_credentials() -> tuple[str | None, str | None, str | None]:
    """Resolve host/token/user from the `glab` CLI's *actual* auth (keyring-aware), not the config
    file — newer glab stores the live token in the OS keyring and leaves a stale config token."""
    try:
        proc = subprocess.run(["glab", "auth", "status", "--show-token"],
                              capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return _glab_config_credentials()
    out = (proc.stdout or "") + (proc.stderr or "")
    host = (re.search(r"Logged in to (\S+)", out) or [None, None])[1]
    user = (re.search(r"\bas (\S+)", out) or [None, None])[1]
    token = (re.search(r"Token:\s*(\S+)", out) or [None, None])[1]
    if not token or set(token) <= {"*"}:  # masked or absent — fall back to the config file
        return _glab_config_credentials()
    return host, token, user


def _glab_git_protocol(host: str | None) -> str | None:
    """The git access method the user configured in glab — per-host block wins over the global
    default. glab lets the user pick ssh or https; review-mate clones over whichever they chose."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "glab-cli" / "config.yml"
    if not cfg.exists():
        return None
    text = cfg.read_text()
    if host:  # the host's own block (indented under `hosts:`) overrides the top-level default
        block = re.search(rf"^\s+{re.escape(host)}:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
        if block:
            per_host = re.search(r"git_protocol:\s*(\w+)", block.group(1))
            if per_host:
                return per_host.group(1)
    top = re.search(r"^git_protocol:\s*(\w+)", text, re.M)
    return top.group(1) if top else None


def _glab_config_credentials() -> tuple[str | None, str | None, str | None]:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "glab-cli" / "config.yml"
    if not cfg.exists():
        return None, None, None
    text = cfg.read_text()
    host = (re.search(r"^\s{2,}([\w.\-]+):\s*$", text, re.M) or [None, None])[1] \
        if "hosts:" in text else None
    token = (re.search(r"token:\s*(\S+)", text) or [None, None])[1]
    user = (re.search(r"user:\s*(\S+)", text) or [None, None])[1]
    return host, token, user


def resolve_gitlab_config() -> GitLabConfig | None:
    base = os.environ.get("REVIEW_MATE_GITLAB_URL", "").rstrip("/")
    token = os.environ.get("REVIEW_MATE_GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN")
    user = os.environ.get("REVIEW_MATE_GITLAB_USER") or os.environ.get("GITLAB_USER")

    g_host, g_token, g_user = (None, None, None)
    if not token or not base or not user:
        g_host, g_token, g_user = _glab_credentials()

    token = token or g_token
    if not token:
        return None  # no credentials → no provider (self-contained baseline still runs)

    if not base:
        host = g_host or "gitlab.com"
        base = f"https://{host}/api/v4"
    host = urlparse(base).netloc
    protocol = (os.environ.get("REVIEW_MATE_GIT_PROTOCOL")
                or _glab_git_protocol(host) or "https").lower()
    return GitLabConfig(base_url=base, token=token, username=(user or g_user or ""),
                        host=host, git_protocol=protocol)


def _token_reloader():
    """Re-resolve the GitLab token from the environment / glab — so a side `glab auth` refresh
    takes effect on a running server without a restart (used to retry after a 401)."""
    cfg = resolve_gitlab_config()
    return cfg.token if cfg else None


def build_gitlab_provider(config: GitLabConfig,
                          client: httpx.AsyncClient | None = None) -> GitLabProvider:
    return GitLabProvider(base_url=config.base_url, token=config.token,
                          username=config.username, host=config.host, client=client,
                          reload_token=_token_reloader, git_protocol=config.git_protocol)


def build_gitlab_writer(config: GitLabConfig,
                        client: httpx.AsyncClient | None = None) -> GitLabWriter:
    return GitLabWriter(base_url=config.base_url, token=config.token,
                        capabilities=dict(GITLAB_CAPABILITIES), client=client,
                        reload_token=_token_reloader)


def build_provider_from_env(client: httpx.AsyncClient | None = None):
    """Host-agnostic entry: select and build a provider from the environment.

    The composition root calls this without naming any host; host selection lives here. Returns
    (provider, resolve_ref) or (None, None) when no host is configured. A second host slots in by
    extending this function, touching no other unit.
    """
    config = resolve_gitlab_config()
    if config is None:
        return None, None
    provider = build_gitlab_provider(config, client)
    return provider, (lambda s: parse_reference(s, config.host))


def build_writer_from_env(client: httpx.AsyncClient | None = None):
    """The write side, host-agnostic: a HostWriter built from the environment, or None.

    Mirrors build_provider_from_env so the composition root names no host. Used to post the
    reviewer's prepared review back to the host.
    """
    config = resolve_gitlab_config()
    if config is None:
        return None
    return build_gitlab_writer(config, client)
