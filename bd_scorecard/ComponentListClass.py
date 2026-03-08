import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from .ComponentClass import Component

from pkg_repo_lookup import (
    fetch_repo_from_deps_dev,
    fetch_repo_from_npm,
    fetch_repo_from_nuget,
    fetch_repo_from_pypi,
    fetch_repo_from_rubygems,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCORECARD_API = "https://api.securityscorecards.dev/projects"
TIMEOUT = 15

ECOSYSTEM_FETCHERS = {
    "npm":      fetch_repo_from_npm,
    "pypi":     fetch_repo_from_pypi,
    "rubygems": fetch_repo_from_rubygems,
    "nuget":    fetch_repo_from_nuget,
    # deps.dev-resolved ecosystems: package part includes version (name/version)
    "maven":    lambda pkg: fetch_repo_from_deps_dev("MAVEN", pkg),
    "cargo":    lambda pkg: fetch_repo_from_deps_dev("CARGO", pkg),
    "golang":   lambda pkg: fetch_repo_from_deps_dev("GO", pkg),
}

# ---------------------------------------------------------------------------
# Package identifier helpers
# ---------------------------------------------------------------------------

def _parse_package_id(pkg_id: str) -> tuple[str, str]:
    """Parse 'ecosystem:package' into (ecosystem, package). Raises ValueError on bad format."""
    if ":" not in pkg_id:
        raise ValueError(
            f"invalid package identifier '{pkg_id}' — expected <ecosystem>:<package> "
            f"(e.g. npm:lodash)"
        )
    ecosystem, _, package = pkg_id.partition(":")
    ecosystem = ecosystem.lower().strip()
    package = package.strip()
    if not ecosystem or not package:
        raise ValueError(f"invalid package identifier '{pkg_id}'")
    if ecosystem not in ECOSYSTEM_FETCHERS:
        raise ValueError(
            f"unsupported ecosystem '{ecosystem}' in '{pkg_id}' — "
            f"supported: {', '.join(sorted(ECOSYSTEM_FETCHERS))}"
        )
    return ecosystem, package


def _repo_url_to_api_path(repo_url: str) -> str:
    """Strip https:// or http:// prefix for use in API paths."""
    url = repo_url.lower()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Scorecard fetch helpers
# ---------------------------------------------------------------------------

def _fetch_scorecard_api(repo_path: str) -> Optional[dict]:
    """Fetch from securityscorecards.dev only. Raises HTTPError on server errors."""
    url = f"{SCORECARD_API}/{repo_path}"
    resp = requests.get(url, timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _parallel_fetch(
    fn, paths: list[str], workers: int, label: str, on_progress
) -> dict[str, Optional[dict]]:
    """Run ``fn(path)`` for each path in parallel; return ``{path: result}``."""
    results: dict[str, Optional[dict]] = {}
    total = len(paths)
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_path = {pool.submit(fn, p): p for p in paths}
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            try:
                results[path] = future.result()
            except Exception as exc:
                results[path] = {"_fetch_error": str(exc)}
            completed += 1
            if completed % 20 == 0 or completed == total:
                msg = f"  {label}: {completed}/{total}"
                if on_progress:
                    on_progress(msg)
                else:
                    print(msg, file=sys.stderr, flush=True)
    return results


# ---------------------------------------------------------------------------
# ComponentList
# ---------------------------------------------------------------------------

class ComponentList:
    def __init__(self):
        self.components: list[Component] = []

    def add(self, comp: Component):
        self.components.append(comp)

    def count(self) -> int:
        return len(self.components)

    def get_pkg_id_map(self) -> dict[str, "Component"]:
        """
        Return a dict mapping every supported pkg_id to its Component.

        When a component has multiple supported origins (rare), each origin
        produces its own pkg_id entry pointing to the same Component object.
        When multiple components share the same pkg_id (also rare), the last
        one wins — the scorecard result is identical for both.
        """
        pkg_map: dict[str, Component] = {}
        for comp in self.components:
            for pkg_id, _ in comp.get_supported_origins():
                pkg_map[pkg_id] = comp
        return pkg_map

    def get_unsupported(self) -> list["Component"]:
        """Return components that have no supported ecosystem origins."""
        return [c for c in self.components if not c.get_supported_origins()]

    def lookup_scorecard(
        self,
        pkg_ids: list[str],
        workers: int = 8,
        on_progress=None,
        pre_resolved: dict[str, str] | None = None,
    ) -> dict[str, dict]:
        """
        Resolve packages to source repos and fetch their scorecard data.

        ``pre_resolved`` is an optional {pkg_id: repo_url} cache; entries in it
        skip the pkg→registry API call and go straight to the scorecard lookup.

        Returns a dict keyed by pkg_id with keys:
          package, ecosystem, repo_url  — always present on success
          scorecard                     — scorecard data dict or None
          error                         — present only when something went wrong
        """
        _pre = pre_resolved or {}
        output: dict[str, dict] = {}
        repo_to_pkgs: dict[str, list[str]] = {}

        # --- Step 1: resolve pkg_id → repo (parallel) ---
        def _resolve_one(pkg_id: str) -> tuple[str, dict]:
            entry: dict = {"package": pkg_id}
            try:
                ecosystem, package = _parse_package_id(pkg_id)
                entry["ecosystem"] = ecosystem
                cached = _pre.get(pkg_id)
                repo_url = cached if cached else ECOSYSTEM_FETCHERS[ecosystem](package)
                entry["repo_url"] = repo_url
                entry["_repo_path"] = _repo_url_to_api_path(repo_url)
            except Exception as exc:
                entry["error"] = str(exc)
            return pkg_id, entry

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_resolve_one, pid): pid for pid in pkg_ids}
            for future in as_completed(futures):
                pid, entry = future.result()
                output[pid] = entry
                repo_path = entry.get("_repo_path")
                if repo_path:
                    repo_to_pkgs.setdefault(repo_path, []).append(pid)

        # --- Step 2: securityscorecards.dev lookup (all repos in parallel) ---
        unique_repos = list(repo_to_pkgs.keys())
        scorecard_by_repo: dict[str, Optional[dict]] = {}
        if unique_repos:
            scorecard_by_repo = _parallel_fetch(
                _fetch_scorecard_api, unique_repos, workers,
                "Scorecard API", on_progress,
            )

        # --- Step 3: merge scorecard data back into per-package entries ---
        for pid, entry in output.items():
            repo_path = entry.pop("_repo_path", None)
            if repo_path is None:
                continue
            sc = scorecard_by_repo.get(repo_path)
            if isinstance(sc, dict) and "_fetch_error" in sc:
                entry["scorecard"] = None
                entry["error"] = f"scorecard API error for {repo_path}: {sc['_fetch_error']}"
            elif sc is None:
                entry["scorecard"] = None
                if "error" not in entry:
                    entry["error"] = f"no scorecard data found for {repo_path}"
            else:
                entry["scorecard"] = sc

        return output
