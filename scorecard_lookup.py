#!/usr/bin/env python3
"""
Look up OpenSSF Scorecard scores for a list of package identifiers.

Resolves package manager identifiers to GitHub/GitLab repos (via pkg_repo_lookup.py),
then fetches their latest scorecard data from the public Scorecard REST API:
  https://api.securityscorecards.dev

No credentials or Google Cloud account are required.

Usage:
  python scorecard_lookup.py npm:lodash pypi:requests nuget:Newtonsoft.Json
  python scorecard_lookup.py --input packages.txt --output results.json
  python scorecard_lookup.py --workers 10 npm:express rubygems:rails

Package identifier format: <ecosystem>:<package>
Supported ecosystems: npm, pypi, rubygems, nuget, maven, cargo, golang
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

from pkg_repo_lookup import (
    fetch_repo_from_deps_dev,
    fetch_repo_from_npm,
    fetch_repo_from_nuget,
    fetch_repo_from_pypi,
    fetch_repo_from_rubygems,
)

SCORECARD_API = "https://api.securityscorecards.dev/projects"
DEFAULT_WORKERS = 8
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
# Helpers
# ---------------------------------------------------------------------------

def parse_package_id(pkg_id: str) -> tuple[str, str]:
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


def repo_url_to_api_path(repo_url: str) -> str:
    """
    Convert a repo URL to the path component used by the Scorecard API.

    https://github.com/foo/bar  →  github.com/foo/bar
    https://gitlab.com/foo/bar  →  gitlab.com/foo/bar
    """
    url = repo_url.lower()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# Scorecard REST API
# ---------------------------------------------------------------------------

def fetch_scorecard(repo_path: str) -> Optional[dict]:
    """
    Fetch scorecard data for one repo from the public REST API.

    ``repo_path`` is the scheme-stripped repo URL, e.g. ``github.com/foo/bar``.
    Returns the parsed JSON dict on success, or None if the repo is not found.
    Raises ``requests.HTTPError`` for unexpected server errors.
    """
    url = f"{SCORECARD_API}/{repo_path}"
    resp = requests.get(url, timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def query_scorecard(repo_paths: list[str], workers: int = DEFAULT_WORKERS, on_progress=None) -> dict[str, Optional[dict]]:
    """
    Fetch scorecard data for multiple repos in parallel.

    Returns a dict keyed by repo_path with the scorecard JSON (or None if not found).
    """
    results: dict[str, Optional[dict]] = {}
    total = len(repo_paths)
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_repo = {
            pool.submit(fetch_scorecard, path): path
            for path in repo_paths
        }
        for future in as_completed(future_to_repo):
            repo_path = future_to_repo[future]
            try:
                results[repo_path] = future.result()
            except Exception as exc:
                results[repo_path] = {"_fetch_error": str(exc)}

            completed += 1
            if completed % 20 == 0 or completed == total:
                msg = f"  Scorecard lookup: {completed}/{total}"
                if on_progress:
                    on_progress(msg)
                else:
                    print(msg, file=sys.stderr, flush=True)

    return results


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def run(raw_ids: list[str], workers: int = DEFAULT_WORKERS, on_progress=None) -> dict[str, dict]:
    """
    Resolve packages and look up their scorecard data.

    Returns a dict keyed by the original package identifier string.
    Each value is a dict with keys:
      package, ecosystem, repo_url  — always present on success
      scorecard                     — scorecard data dict or None
      error                         — present only when something went wrong
    """
    output: dict[str, dict] = {}
    repo_to_pkgs: dict[str, list[str]] = {}   # repo_path → [pkg_id, ...]

    # --- step 1: parse + resolve package → repo (parallel) ---
    def _resolve_one(pkg_id: str) -> tuple[str, dict]:
        entry: dict = {"package": pkg_id}
        try:
            ecosystem, package = parse_package_id(pkg_id)
            entry["ecosystem"] = ecosystem
            repo_url = ECOSYSTEM_FETCHERS[ecosystem](package)
            entry["repo_url"] = repo_url
            entry["_repo_path"] = repo_url_to_api_path(repo_url)
        except Exception as exc:
            entry["error"] = str(exc)
        return pkg_id, entry

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_resolve_one, pkg_id): pkg_id for pkg_id in raw_ids}
        for future in as_completed(futures):
            pkg_id, entry = future.result()
            output[pkg_id] = entry
            repo_path = entry.get("_repo_path")
            if repo_path:
                repo_to_pkgs.setdefault(repo_path, []).append(pkg_id)

    # --- step 2: parallel Scorecard API lookups (deduplicated by repo) ---
    unique_repos = list(repo_to_pkgs.keys())
    scorecard_by_repo: dict[str, Optional[dict]] = {}
    if unique_repos:
        scorecard_by_repo = query_scorecard(unique_repos, workers=workers, on_progress=on_progress)

    # --- step 3: merge scorecard data back into per-package entries ---
    for pkg_id, entry in output.items():
        repo_path = entry.pop("_repo_path", None)
        if repo_path is None:
            continue  # resolution failed earlier; error already recorded

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Look up OpenSSF Scorecard scores for a list of packages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "packages",
        nargs="*",
        metavar="ECOSYSTEM:PACKAGE",
        help="one or more package identifiers (e.g. npm:lodash pypi:requests)",
    )
    parser.add_argument(
        "--input", "-i",
        metavar="FILE",
        help="read package identifiers from FILE (one per line; # comments are ignored)",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="write JSON results to FILE instead of stdout",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        metavar="N",
        help=f"parallel API workers (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact (single-line) JSON instead of pretty-printed",
    )
    args = parser.parse_args()

    # Collect identifiers from positional args and/or input file
    raw_ids: list[str] = list(args.packages)
    if args.input:
        try:
            with open(args.input) as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        raw_ids.append(line)
        except OSError as exc:
            sys.exit(f"error: {exc}")

    if not raw_ids:
        parser.print_help(sys.stderr)
        sys.exit(1)

    try:
        results = run(raw_ids, workers=args.workers)
    except Exception as exc:
        sys.exit(f"error: {exc}")

    indent = None if args.compact else 2
    payload = json.dumps(results, indent=indent)

    if args.output:
        try:
            with open(args.output, "w") as fh:
                fh.write(payload)
                fh.write("\n")
            print(f"wrote {len(results)} result(s) to {args.output}", file=sys.stderr)
        except OSError as exc:
            sys.exit(f"error: {exc}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
