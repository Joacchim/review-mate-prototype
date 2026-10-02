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

from review_mate.forges import Forges
from review_mate.host.base import GITLAB_CAPABILITIES, parse_reference
from review_mate.host.github import (
    GITHUB_CAPABILITIES, GitHubProvider, GitHubWriter, parse_github_reference,
)
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
    token = (re.search(r"Token(?: found)?:\s*(\S+)", out) or [None, None])[1]
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


class GitHubConfig:
    def __init__(self, base_url: str, token: str, username: str, host: str,
                 git_protocol: str = "https"):
        self.base_url = base_url          # …/api/v3 on Enterprise, api.github.com otherwise
        self.token = token
        self.username = username
        self.host = host                  # the forge people name, not the API endpoint
        self.git_protocol = git_protocol


def _gh_credentials() -> tuple[str | None, str | None]:
    """What `gh auth` left behind, so a reviewer already logged in configures nothing.

    Mirrors the glab fallback: the token and the account, read rather than asked for.
    """
    cfg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gh" / "hosts.yml"
    if not cfg.exists():
        return None, None
    token = user = None
    try:
        for line in cfg.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("oauth_token:"):
                token = token or stripped.partition(":")[2].strip()
            elif stripped.startswith("user:"):
                user = user or stripped.partition(":")[2].strip()
    except OSError:
        return None, None
    return token, user


def resolve_github_config() -> GitHubConfig | None:
    base = os.environ.get("REVIEW_MATE_GITHUB_URL", "").rstrip("/")
    token = os.environ.get("REVIEW_MATE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    user = os.environ.get("REVIEW_MATE_GITHUB_USER") or os.environ.get("GITHUB_USER")
    if not token or not user:
        gh_token, gh_user = _gh_credentials()
        token, user = token or gh_token, user or gh_user
    if not token:
        return None            # no credentials → no GitHub, and the rest still runs
    if not base:
        base = "https://api.github.com"
    netloc = urlparse(base).netloc
    # the review model's host is the forge people name, not the API endpoint in front of it
    host = "github.com" if netloc == "api.github.com" else netloc
    protocol = (os.environ.get("REVIEW_MATE_GIT_PROTOCOL") or "https").lower()
    return GitHubConfig(base_url=base, token=token, username=(user or ""), host=host,
                        git_protocol=protocol)


def build_github_provider(config: GitHubConfig, client: httpx.AsyncClient | None = None):
    return GitHubProvider(base_url=config.base_url, token=config.token,
                          username=config.username, host=config.host,
                          client=client, git_protocol=config.git_protocol)


def build_github_writer(config: GitHubConfig, client: httpx.AsyncClient | None = None):
    return GitHubWriter(base_url=config.base_url, token=config.token, host=config.host,
                        capabilities=dict(GITHUB_CAPABILITIES), client=client)


def build_provider_from_env(client: httpx.AsyncClient | None = None):
    """Host-agnostic entry: build every configured forge from the environment.

    The composition root names no host; selection lives here. Returns (forges, resolve_ref), or
    (None, None) when nothing is configured — the self-contained baseline, where a branch on this
    machine is still reviewable.

    `resolve_ref` tries each forge's own parser. They cannot collide: a pull request URL carries
    `/pull/`, a merge request URL `/-/merge_requests/`, and the shorthands are told apart by the
    separator each forge's own users already write — `owner/repo#12` against `group/proj!12`.
    """
    forges, parsers = {}, []
    gitlab = resolve_gitlab_config()
    if gitlab is not None:
        forges[gitlab.host] = build_gitlab_provider(gitlab, client)
        parsers.append(lambda s, h=gitlab.host: parse_reference(s, h))
    github = resolve_github_config()
    if github is not None:
        forges[github.host] = build_github_provider(github, client)
        parsers.append(lambda s, h=github.host: parse_github_reference(s, h))
    if not forges:
        return None, None

    def resolve_ref(s: str):
        for parse in parsers:
            ref = parse(s)
            if ref is not None:
                return ref
        return None

    return Forges(forges), resolve_ref


def build_writer_from_env(client: httpx.AsyncClient | None = None):
    """The write side, host-agnostic: a HostWriter built from the environment, or None.

    Mirrors build_provider_from_env so the composition root names no host, and is keyed the same
    way: a review is written back to the forge it was read from.
    """
    writers = {}
    gitlab = resolve_gitlab_config()
    if gitlab is not None:
        writers[gitlab.host] = build_gitlab_writer(gitlab, client)
    github = resolve_github_config()
    if github is not None:
        writers[github.host] = build_github_writer(github, client)
    return Forges(writers) if writers else None
