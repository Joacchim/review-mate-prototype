"""GitLab connection config — resolve credentials portably (env first, then glab), build providers.

Self-contained by default (D11): reads `REVIEW_MATE_GITLAB_*` / `GITLAB_*` env, then falls back to
the local `glab` CLI's config for ambient credentials (D8). No token configured → no provider.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from review_mate.host.base import GITLAB_CAPABILITIES, parse_reference
from review_mate.host.gitlab import GitLabProvider, GitLabWriter


class GitLabConfig:
    def __init__(self, base_url: str, token: str, username: str, host: str):
        self.base_url = base_url          # …/api/v4
        self.token = token
        self.username = username
        self.host = host


def _glab_credentials() -> tuple[str | None, str | None, str | None]:
    """Best-effort read of host/token/user from the glab CLI config (ambient creds)."""
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
    return GitLabConfig(base_url=base, token=token, username=(user or g_user or ""), host=host)


def build_gitlab_provider(config: GitLabConfig,
                          client: httpx.AsyncClient | None = None) -> GitLabProvider:
    return GitLabProvider(base_url=config.base_url, token=config.token,
                          username=config.username, host=config.host, client=client)


def build_gitlab_writer(config: GitLabConfig,
                        client: httpx.AsyncClient | None = None) -> GitLabWriter:
    return GitLabWriter(base_url=config.base_url, token=config.token,
                        capabilities=dict(GITLAB_CAPABILITIES), client=client)


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
