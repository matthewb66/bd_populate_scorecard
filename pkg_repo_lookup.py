#!/usr/bin/env python3
"""
Replicates the scorecard package manager → GitHub repo resolution logic.

Supports: npm, PyPI, RubyGems, NuGet

Usage:
    python pkg_repo_lookup.py --npm <package>
    python pkg_repo_lookup.py --pypi <package>
    python pkg_repo_lookup.py --rubygems <package>
    python pkg_repo_lookup.py --nuget <package>
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

TIMEOUT = 15

# ---------------------------------------------------------------------------
# Shared regex matchers (used by PyPI; defined in package_managers.go:32-34)
# ---------------------------------------------------------------------------

_GITHUB_DOMAIN = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)", re.IGNORECASE)
_GITHUB_SUBDOMAIN = re.compile(r"^https?://([^.]+)\.github\.io/([^/]+)", re.IGNORECASE)
_GITLAB_DOMAIN = re.compile(r"^https?://gitlab\.com/([^/]+)/([^/]+)", re.IGNORECASE)

# Allowed for nuget project URLs (package_managers.go:227)
_SUPPORTED_PROJECT_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github|gitlab)\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_./\-]+)$"
)


def _make_github_repo(match) -> str:
    """Build a normalised github.com URL from a regex match (groups 1 and 2)."""
    if not match:
        return ""
    user = match.group(1).lower()
    repo = match.group(2).lower().removesuffix(".git")
    if user == "sponsors":
        return ""
    return f"https://github.com/{user}/{repo}"


# ---------------------------------------------------------------------------
# NPM  (package_managers.go:128-147)
# ---------------------------------------------------------------------------

def fetch_repo_from_npm(package: str) -> str:
    url = f"https://registry.npmjs.org/{package}/latest"
    resp = requests.get(url, timeout=TIMEOUT)
    if resp.status_code == 404:
        raise ValueError(f"npm package not found: {package}")
    resp.raise_for_status()

    data = resp.json()
    repo_url: str = data.get("repository", {}).get("url", "")
    if not repo_url:
        raise ValueError(f"could not find source repo for npm package: {package}")

    # Strip git+ prefix and .git suffix
    repo_url = repo_url.removeprefix("git+").removesuffix(".git")
    return repo_url


# ---------------------------------------------------------------------------
# PyPI  (package_managers.go:149-191)
# ---------------------------------------------------------------------------

def _pypi_matchers(url: str) -> str:
    """Return a normalised repo URL if the URL matches a known forge, else ''."""
    m = _GITHUB_DOMAIN.match(url)
    if m:
        return _make_github_repo(m)

    m = _GITHUB_SUBDOMAIN.match(url)
    if m:
        return _make_github_repo(m)

    m = _GITLAB_DOMAIN.match(url)
    if m:
        user = m.group(1).lower()
        repo = m.group(2).lower()
        return f"https://gitlab.com/{user}/{repo}"

    return ""


def fetch_repo_from_pypi(package: str) -> str:
    url = f"https://pypi.org/pypi/{package}/json"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()

    data = resp.json()
    info = data.get("info", {})
    project_urls: dict = dict(info.get("project_urls") or {})
    # Merge the top-level project_url into the map (package_managers.go:156)
    project_urls["__project_url__"] = info.get("project_url") or ""

    valid_url = ""
    for candidate in project_urls.values():
        if not candidate:
            continue
        repo = _pypi_matchers(candidate)
        if not repo:
            continue
        if valid_url == "":
            valid_url = repo
        elif valid_url != repo:
            raise ValueError(
                f"found too many possible source repos for pypi package: {package}"
            )

    if not valid_url:
        raise ValueError(f"could not find source repo for pypi package: {package}")
    return valid_url


# ---------------------------------------------------------------------------
# RubyGems  (package_managers.go:194-211)
# ---------------------------------------------------------------------------

def fetch_repo_from_rubygems(package: str) -> str:
    url = f"https://rubygems.org/api/v1/gems/{package}.json"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()

    data = resp.json()
    source_uri: str = data.get("source_code_uri") or ""
    if not source_uri:
        raise ValueError(f"could not find source repo for ruby gem: {package}")
    return source_uri


# ---------------------------------------------------------------------------
# NuGet  (cmd/internal/nuget/client.go)
# ---------------------------------------------------------------------------

def _nuget_base_urls() -> tuple[str, str]:
    """Return (packageBaseURL, registrationBaseURL) from the NuGet service index."""
    resp = requests.get("https://api.nuget.org/v3/index.json", timeout=TIMEOUT)
    resp.raise_for_status()
    resources = resp.json().get("resources", [])

    pkg_base = reg_base = ""
    for r in resources:
        rtype = r.get("@type", "")
        if rtype == "PackageBaseAddress/3.0.0":
            pkg_base = r["@id"]
        elif rtype == "RegistrationsBaseUrl/3.6.0":
            reg_base = r["@id"]

    if not pkg_base:
        raise ValueError("failed to find PackageBaseAddress/3.0.0 in NuGet index")
    if not reg_base:
        raise ValueError("failed to find RegistrationsBaseUrl/3.6.0 in NuGet index")
    return pkg_base, reg_base


def _parse_nuget_semver(version: str) -> tuple[str, str]:
    """Return (base, pre_release_suffix) per NuGet semver rules (client.go:236-243)."""
    version = version.split("+")[0]          # strip build metadata
    parts = version.split("-", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def _nuget_latest_listed_version(reg_base: str, pkg_lower: str) -> str:
    """
    Find the latest listed, non-pre-release version from the registration catalog.
    Iterates pages from the end; fetches inline pages on demand (client.go:56-79).
    """
    url = f"{reg_base}{pkg_lower}/index.json"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    catalog = resp.json()

    pages = catalog.get("items", [])
    for page in reversed(pages):
        items = page.get("items")
        if items is None:
            # Page is not inline — fetch it
            page_resp = requests.get(page["@id"], timeout=TIMEOUT)
            page_resp.raise_for_status()
            items = page_resp.json().get("items", [])

        for item in reversed(items):
            entry = item.get("catalogEntry", {})
            version: str = entry.get("version", "")
            # Default for 'listed' is True when the field is absent (client.go:98)
            listed: bool = entry.get("listed", True)
            _, pre = _parse_nuget_semver(version)
            if listed and not pre.strip():
                return _parse_nuget_semver(version)[0]

    raise ValueError(f"failed to get a listed version for nuget package: {pkg_lower}")


def fetch_repo_from_nuget(package: str) -> str:
    pkg_base, reg_base = _nuget_base_urls()
    pkg_lower = package.lower()
    version = _nuget_latest_listed_version(reg_base, pkg_lower)

    nuspec_url = f"{pkg_base}{pkg_lower}/{version}/{pkg_lower}.nuspec"
    resp = requests.get(nuspec_url, timeout=TIMEOUT)
    resp.raise_for_status()

    # Parse XML nuspec — check repository/@url first, then projectUrl (client.go:112-123)
    root = ET.fromstring(resp.text)
    ns_match = re.match(r"\{([^}]+)\}", root.tag)
    ns = f"{{{ns_match.group(1)}}}" if ns_match else ""

    metadata = root.find(f"{ns}metadata")
    if metadata is None:
        raise ValueError(f"nuspec metadata is empty for nuget package: {package}")

    candidates = []
    repo_elem = metadata.find(f"{ns}repository")
    if repo_elem is not None:
        candidates.append(repo_elem.get("url", "").strip())
    proj_url = metadata.findtext(f"{ns}projectUrl") or ""
    candidates.append(proj_url.strip())

    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.removesuffix("/").removesuffix(".git")
        if _SUPPORTED_PROJECT_URL.match(candidate):
            return candidate

    raise ValueError(f"source repo is not defined for nuget package: {package}")


# ---------------------------------------------------------------------------
# deps.dev  (REST API v3 — https://docs.deps.dev/api/v3/)
# ---------------------------------------------------------------------------

def fetch_repo_from_deps_dev(system: str, package_with_version: str) -> str:
    """
    Look up the source repository for a package via the deps.dev REST API.

    ``system`` is the deps.dev ecosystem name in uppercase (e.g. ``'MAVEN'``,
    ``'CARGO'``, ``'GO'``).

    ``package_with_version`` is the BD ``externalId`` with the version retained,
    where the version is the last ``'/'``-delimited segment (e.g.
    ``'org.apache.commons:commons-lang3/3.12.0'``).

    Returns the source repository URL.
    Raises ``ValueError`` if the package is not found or has no source repo.
    """
    parts = package_with_version.rsplit('/', 1)
    if len(parts) != 2:
        raise ValueError(
            f"cannot parse package/version from '{package_with_version}'"
        )
    pkg_name, version = parts

    url = (
        f"https://api.deps.dev/v3/systems/{system}/packages/"
        f"{quote(pkg_name, safe='')}/versions/{quote(version, safe='')}"
    )
    resp = requests.get(url, timeout=TIMEOUT)
    if resp.status_code == 404:
        raise ValueError(
            f"package not found on deps.dev: {system}:{pkg_name}@{version}"
        )
    resp.raise_for_status()
    data = resp.json()

    # Prefer a relatedProject with relationType == SOURCE_REPO
    for project in data.get('relatedProjects', []):
        if project.get('relationType') == 'SOURCE_REPO':
            project_id = project.get('projectKey', {}).get('id', '')
            if project_id:
                return f"https://{project_id}"

    # Fallback: links array entry with 'source' in the label
    for link in data.get('links', []):
        if 'source' in link.get('label', '').lower():
            return link['url']

    raise ValueError(
        f"no source repository found on deps.dev for {system}:{pkg_name}@{version}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve a package manager package to its upstream GitHub/GitLab repo."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--npm", metavar="PACKAGE")
    group.add_argument("--pypi", metavar="PACKAGE")
    group.add_argument("--rubygems", metavar="PACKAGE")
    group.add_argument("--nuget", metavar="PACKAGE")
    args = parser.parse_args()

    try:
        if args.npm:
            print(fetch_repo_from_npm(args.npm))
        elif args.pypi:
            print(fetch_repo_from_pypi(args.pypi))
        elif args.rubygems:
            print(fetch_repo_from_rubygems(args.rubygems))
        elif args.nuget:
            print(fetch_repo_from_nuget(args.nuget))
    except (ValueError, requests.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
