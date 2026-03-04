# Maps Black Duck externalNamespace values to scorecard_lookup ecosystem names.
# Only namespaces that pkg_repo_lookup.py can resolve are listed here.
BD_NAMESPACE_TO_ECOSYSTEM = {
    "npmjs":     "npm",
    "pypi":      "pypi",
    "rubygems":  "rubygems",
    "nuget":     "nuget",
}

# Ecosystems resolved via deps.dev (requires version — full externalId is kept).
BD_NAMESPACE_TO_DEPS_DEV = {
    "maven":  "maven",
    "cargo":  "cargo",
    "golang": "golang",
}


def _pkg_name_from_external_id(external_id: str) -> str:
    """
    Extract the package name from a Black Duck externalId string.

    Black Duck encodes the version as the last '/'-delimited segment:
      lodash/4.17.21            →  lodash
      @angular/core/15.0.0      →  @angular/core
      requests/2.28.0           →  requests
      Newtonsoft.Json/13.0.1    →  Newtonsoft.Json

    For maven-style IDs (groupId:artifactId/version) the whole groupId:artifactId
    is returned, but those namespaces are not in BD_NAMESPACE_TO_ECOSYSTEM so they
    are never used.
    """
    parts = external_id.split('/')
    if len(parts) < 2:
        return external_id
    return '/'.join(parts[:-1])


class Component:
    def __init__(self, data: dict):
        self.data = data
        self.name = data.get('componentName', '')
        self.version = data.get('componentVersionName', '')

    def get_supported_origins(self) -> list[tuple[str, str]]:
        """
        Return a list of (pkg_id, ecosystem) tuples for each origin whose
        namespace is supported by scorecard_lookup / pkg_repo_lookup.

        A single component may have origins in multiple registries; we return
        all supported ones so the caller can decide which to use.

        For registry-resolved ecosystems (npm, pypi, rubygems, nuget) the
        version is stripped from the pkg_id; the registry API resolves the
        latest version.  For deps.dev-resolved ecosystems (maven, cargo,
        golang) the full externalId including version is retained because
        the deps.dev API requires an explicit version.
        """
        results = []
        seen_pkg_ids: set[str] = set()

        for origin in self.data.get('origins', []):
            namespace = origin.get('externalNamespace', '')
            ext_id = origin.get('externalId', '')
            if not ext_id:
                continue

            ecosystem = BD_NAMESPACE_TO_ECOSYSTEM.get(namespace)
            if ecosystem:
                pkg_id = f"{ecosystem}:{_pkg_name_from_external_id(ext_id)}"
            else:
                ecosystem = BD_NAMESPACE_TO_DEPS_DEV.get(namespace)
                if not ecosystem:
                    continue
                # BD Maven uses groupId:artifactId:version (colon-separated);
                # all other deps.dev ecosystems use name/version (slash-separated).
                # Normalise to name/version so fetch_repo_from_deps_dev can always
                # split on the last '/'.
                sep = ':' if namespace == 'maven' else '/'
                parts = ext_id.rsplit(sep, 1)
                if len(parts) != 2:
                    continue
                pkg_id = f"{ecosystem}:{parts[0]}/{parts[1]}"

            if pkg_id not in seen_pkg_ids:
                seen_pkg_ids.add(pkg_id)
                results.append((pkg_id, ecosystem))

        return results

    def unsupported_namespaces(self) -> list[str]:
        """Return the list of origin namespaces that are not supported."""
        unsupported = []
        for origin in self.data.get('origins', []):
            ns = origin.get('externalNamespace', '')
            if ns and ns not in BD_NAMESPACE_TO_ECOSYSTEM and ns not in BD_NAMESPACE_TO_DEPS_DEV:
                if ns not in unsupported:
                    unsupported.append(ns)
        return unsupported
