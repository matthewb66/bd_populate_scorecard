import sys

from blackduck import Client

from .ComponentClass import Component
from .ComponentListClass import ComponentList

# Accept header for the BOM components endpoint
_BOM_ACCEPT = "application/vnd.blackducksoftware.bill-of-materials-6+json"


class BOM:
    def __init__(self, conf):
        self.complist = ComponentList()

        try:
            self.bd = Client(
                token=conf.bd_api,
                base_url=conf.bd_url,
                verify=(not conf.bd_trustcert),
                timeout=60,
            )
        except Exception as exc:
            conf.logger.error(f"Failed to create Black Duck client: {exc}")
            sys.exit(-1)

        conf.logger.info(f"Working on project '{conf.bd_project}' version '{conf.bd_version}'")

        self.bdver_dict = self._get_project(conf)
        res = self.bd.list_resources(self.bdver_dict)
        self.projver = res['href']

        self._fetch_components(conf)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_paginated_data(self, url: str, accept_hdr: str) -> list:
        """Fetch all pages from a Black Duck paginated endpoint."""
        headers = {'accept': accept_hdr}
        res = self.bd.get_json(url + "?limit=1000", headers=headers)

        if 'totalCount' not in res or 'items' not in res:
            return []

        total = res['totalCount']
        items = list(res['items'])
        downloaded = len(items)

        while downloaded < total:
            paged_url = f"{url}?limit=1000&offset={downloaded}"
            res = self.bd.get_json(paged_url, headers=headers)
            if 'totalCount' not in res or 'items' not in res:
                break
            items += res['items']
            downloaded += len(res['items'])

        return items

    def _get_project(self, conf):
        """Find the matching project version dict or exit."""
        params = {'q': f"name:{conf.bd_project}", 'sort': 'name'}

        ver_dict = None
        projects = self.bd.get_resource('projects', params=params)
        for p in projects:
            if p['name'] == conf.bd_project:
                versions = self.bd.get_resource('versions', parent=p)
                for v in versions:
                    if v['versionName'] == conf.bd_version:
                        ver_dict = v
                        break
                break
        else:
            conf.logger.error(f"Project '{conf.bd_project}' not found on {conf.bd_url}")
            sys.exit(2)

        if ver_dict is None:
            conf.logger.error(
                f"Version '{conf.bd_version}' not found in project '{conf.bd_project}'"
            )
            sys.exit(2)

        return ver_dict

    def _fetch_components(self, conf):
        """Fetch all BOM components and populate self.complist."""
        comp_url = f"{self.projver}/components"
        raw_items = self._get_paginated_data(comp_url, _BOM_ACCEPT)

        skipped_unresolved = 0
        for item in raw_items:
            # Skip components that have no resolved version (sub-project rows, etc.)
            if 'componentVersion' not in item:
                skipped_unresolved += 1
                continue
            self.complist.add(Component(item))

        conf.logger.info(
            f"Fetched {self.complist.count()} resolved components "
            f"({skipped_unresolved} unresolved skipped)"
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_components(self) -> ComponentList:
        return self.complist
