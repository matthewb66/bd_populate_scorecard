import json
import math
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from blackduck import Client

_ADMIN_ACCEPT       = "application/vnd.blackducksoftware.admin-4+json"
_ADMIN_CONTENT_TYPE = "application/vnd.blackducksoftware.admin-4+json"
_COMP_CONTENT_TYPE  = "application/vnd.blackducksoftware.component-detail-5+json"

# All scorecard check names that can be created as custom fields.
VALID_SC_FIELDS = [
    "SC-Maintained",
    "SC-Dangerous-Workflow",
    "SC-Code-Review",
    "SC-Binary-Artifacts",
    "SC-Token-Permissions",
    "SC-CII-Best-Practices",
    "SC-Security-Policy",
    "SC-License",
    "SC-Fuzzing",
    "SC-Signed-Releases",
    "SC-Branch-Protection",
    "SC-Packaging",
    "SC-Pinned-Dependencies",
    "SC-SAST",
]

SC_OVERALL    = "SC-Overall"
SC_DATE       = "SC-Date"
SC_SOURCEREPO = "SC-Sourcerepo"

# Human-readable descriptions for each field.
_DESCRIPTIONS = {
    "SC-Overall":              "OpenSSF Scorecard overall score (0-10)",
    "SC-Date":                 "Date of the most recent OpenSSF Scorecard scan",
    "SC-Sourcerepo":           "Resolved source repository used for the OpenSSF Scorecard lookup",
    "SC-Maintained":           "Scorecard: Is the project actively maintained?",
    "SC-Dangerous-Workflow":   "Scorecard: Are dangerous GitHub Actions workflow patterns avoided?",
    "SC-Code-Review":          "Scorecard: Are code changes reviewed before merging?",
    "SC-Binary-Artifacts":     "Scorecard: Does the project avoid binary artifacts in its source?",
    "SC-Token-Permissions":    "Scorecard: Does the project use minimal GitHub token permissions?",
    "SC-CII-Best-Practices":   "Scorecard: Has the project earned an OpenSSF Best Practices Badge?",
    "SC-Security-Policy":      "Scorecard: Has the project published a security policy?",
    "SC-License":              "Scorecard: Does the project declare a license?",
    "SC-Fuzzing":              "Scorecard: Does the project use fuzzing tools?",
    "SC-Signed-Releases":      "Scorecard: Does the project cryptographically sign releases?",
    "SC-Branch-Protection":    "Scorecard: Does the project use branch protection rules?",
    "SC-Packaging":            "Scorecard: Does the project build and publish official packages?",
    "SC-Pinned-Dependencies":  "Scorecard: Are the project's dependencies pinned to specific versions?",
    "SC-SAST":                 "Scorecard: Does the project use static application security testing?",
}

# Field type overrides — everything not listed here defaults to DROPDOWN.
_FIELD_TYPES = {
    SC_DATE:       "DATE",
    SC_SOURCEREPO: "TEXT",
}

# Load dropdown options from options.json (project root, one level above this package).
_OPTIONS_FILE = pathlib.Path(__file__).parent.parent / "options.json"


def _load_options() -> list[dict]:
    """
    Read options.json and return a list of {position, label} dicts suitable
    for use as dropdown options (positions 0–10).
    """
    try:
        with open(_OPTIONS_FILE) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot load options from {_OPTIONS_FILE}: {exc}") from exc

    return [
        {"position": item["position"], "label": item["label"]}
        for item in data.get("items", [])
    ]



def _score_to_label(score: float) -> str | None:
    """
    Map a scorecard float score to a DROPDOWN option label string ('1'–'10').
    Score 0 maps to '1' (minimum available option).
    Returns None for invalid scores (< 0).
    """
    if score < 0:
        return None
    return str(max(1, min(int(math.floor(score)), 10)))


def _date_to_bd(date_str: str) -> str | None:
    """
    Convert a scorecard ISO-8601 date string to the format BD DATE fields expect.
    Returns None if the string is empty or unparseable.
    """
    if not date_str:
        return None
    # BD DATE fields require a full ISO-8601 datetime string.
    # "2026-02-28T02:18:57Z" → "2026-02-28T00:00:00.000Z"
    return date_str[:10] + "T00:00:00.000Z"


class CustomFields:
    def __init__(self, bd: Client, conf):
        self.bd = bd
        self.conf = conf
        self._fields_url: str | None = None
        self._options: list[dict] = _load_options()

    # ------------------------------------------------------------------
    # Internal helpers — field definitions
    # ------------------------------------------------------------------

    def _get_fields_url(self) -> str:
        """
        Discover the URL for managing Component-level custom fields.

        Calls GET /api/custom-fields/objects, finds the object whose name is
        "component", then follows its ``custom-field-list`` link.
        """
        if self._fields_url:
            return self._fields_url

        url = f"{self.bd.base_url}/api/custom-fields/objects"
        res = self.bd.get_json(url, headers={"accept": _ADMIN_ACCEPT})

        for obj in res.get("items", []):
            if obj.get("name", "").lower() == "component":
                for link in obj.get("_meta", {}).get("links", []):
                    if link.get("rel") == "custom-field-list":
                        self._fields_url = link["href"]
                        return self._fields_url
                # Fallback: construct from the object's own href
                self._fields_url = obj["_meta"]["href"].rstrip("/") + "/fields"
                return self._fields_url

        self.conf.logger.error(
            "Could not find a 'Component' object in /api/custom-fields/objects — "
            "check that the API token has admin permissions"
        )
        sys.exit(2)

    def _get_existing_labels(self) -> set[str]:
        """Return the set of labels for all currently defined Component custom fields."""
        url = self._get_fields_url()
        res = self.bd.get_json(url, headers={"accept": _ADMIN_ACCEPT})
        return {item["label"] for item in res.get("items", [])}

    # ------------------------------------------------------------------
    # DROPDOWN option management
    # ------------------------------------------------------------------

    def _field_options_url(self, field_id: str) -> str:
        """Return the URL for the options sub-resource of a specific field."""
        return self._get_fields_url().rstrip("/") + f"/{field_id}/options"

    def _fetch_field_options(self, field_id: str) -> list[dict]:
        """GET the current options for a field. Returns list of option dicts."""
        try:
            url = self._field_options_url(field_id) + "?limit=100"
            res = self.bd.get_json(url, headers={"accept": _ADMIN_ACCEPT})
            return res.get("items", [])
        except Exception as exc:
            self.conf.logger.debug(f"  Could not fetch options for field {field_id}: {exc}")
            return []

    def _post_options_for_field(self, field_id: str) -> None:
        """
        Ensure all score options (0–10) exist on a DROPDOWN field.

        POSTs any options not yet present.  ``initialOptions`` in the field
        creation body may be silently ignored by some BD versions, so options
        are always posted explicitly after field creation and before upload.
        """
        existing = self._fetch_field_options(field_id)
        existing_positions = {item.get("position") for item in existing}

        missing = [opt for opt in self._options if opt["position"] not in existing_positions]
        if not missing:
            self.conf.logger.debug(f"  Field {field_id}: all {len(self._options)} options present")
            return

        self.conf.logger.info(
            f"  Field {field_id}: creating {len(missing)} missing option(s) …"
        )
        options_url = self._field_options_url(field_id)
        for opt in missing:
            resp = self.bd.session.post(
                options_url,
                data=json.dumps({"position": opt["position"], "label": opt["label"]}),
                headers={
                    "Content-Type": _ADMIN_CONTENT_TYPE,
                    "Accept": _ADMIN_ACCEPT,
                },
            )
            if resp.status_code not in (200, 201):
                # 412 "duplicate label" means the option already exists — not an error
                if resp.status_code == 412 and "duplicate" in resp.text.lower():
                    self.conf.logger.debug(
                        f"  Option '{opt['label']}' already exists for field {field_id}"
                    )
                else:
                    self.conf.logger.warning(
                        f"  Could not create option '{opt['label']}' for field {field_id}: "
                        f"HTTP {resp.status_code} — {resp.text[:100]}"
                    )

    def ensure_dropdown_options(self) -> None:
        """
        For every SC-* DROPDOWN field, verify all score options (0–10) exist,
        creating any that are missing.

        Call this once before uploading values to component custom fields.
        """
        url = self._get_fields_url()
        res = self.bd.get_json(url, headers={"accept": _ADMIN_ACCEPT})
        dropdown_items = [
            item for item in res.get("items", [])
            if item.get("label", "").startswith("SC-") and item.get("type") == "DROPDOWN"
        ]
        if not dropdown_items:
            return
        self.conf.logger.info(
            f"Verifying options for {len(dropdown_items)} SC-* DROPDOWN field(s) …"
        )
        for item in dropdown_items:
            href = item.get("_meta", {}).get("href", "")
            field_id = href.rstrip("/").split("/")[-1]
            if field_id:
                self._post_options_for_field(field_id)

    # ------------------------------------------------------------------
    # Field creation
    # ------------------------------------------------------------------

    def _create_one(self, label: str) -> bool:
        """POST a single custom field (DROPDOWN or DATE). Returns True on success."""
        url = self._get_fields_url()
        field_type = _FIELD_TYPES.get(label, "DROPDOWN")
        payload = {
            "position": 0,
            "label": label,
            "description": _DESCRIPTIONS.get(label, f"OpenSSF Scorecard: {label}"),
            "type": field_type,
            "active": True,
            "required": False,
        }
        if field_type == "DROPDOWN":
            payload["initialOptions"] = self._options
        resp = self.bd.session.post(
            url,
            data=json.dumps(payload),
            headers={
                "Content-Type": _ADMIN_CONTENT_TYPE,
                "Accept": _ADMIN_ACCEPT,
            },
        )
        if resp.status_code == 201:
            return True
        self.conf.logger.error(
            f"Failed to create custom field '{label}': "
            f"HTTP {resp.status_code} — {resp.text[:200]}"
        )
        return False

    # ------------------------------------------------------------------
    # Internal helpers — component value upload
    # ------------------------------------------------------------------

    def _put_component_field(
        self, component_id: str, field_id: str, value: str, label: str = ""
    ) -> bool:
        """
        PUT a single custom field value on a specific component.

        ``component_id`` is the UUID from the component URL.
        ``field_id``     is the ID from the field definition href.
        ``value``        is the string value to set.
        """
        url = f"{self.bd.base_url}/api/components/{component_id}/custom-fields/{field_id}"
        self.conf.logger.debug(
            f"  PUT {url} — body={json.dumps({'values': [value]})}"
        )
        resp = self.bd.session.put(
            url,
            data=json.dumps({"values": [value]}),
            headers={
                "Content-Type": _COMP_CONTENT_TYPE,
                "Accept": _COMP_CONTENT_TYPE,
            },
        )
        if resp.status_code == 200:
            return True
        self.conf.logger.warning(
            f"  Failed to set {label or field_id} on component {component_id}"
            f" — HTTP {resp.status_code}: {resp.text[:200]}"
        )
        return False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def create_fields(self, requested: list[str]) -> None:
        """
        Create the requested custom fields plus SC-Overall and SC-Date on the
        Component object, skipping any that already exist.

        ``requested`` should be a (possibly empty) list of names from VALID_SC_FIELDS.
        SC-Overall and SC-Date are always included.
        """
        always = [SC_OVERALL, SC_DATE, SC_SOURCEREPO]
        extras = [f for f in requested if f not in always]
        wanted = always + extras

        self.conf.logger.info(
            f"Loaded {len(self._options)} dropdown options from {_OPTIONS_FILE.name}"
        )
        self.conf.logger.info("Checking existing Component custom fields …")
        existing = self._get_existing_labels()
        if existing:
            self.conf.logger.info(
                f"Found {len(existing)} existing custom field(s): "
                + ", ".join(sorted(existing))
            )

        to_create = [label for label in wanted if label not in existing]
        to_skip   = [label for label in wanted if label in existing]

        if to_skip:
            self.conf.logger.info(
                f"Skipping {len(to_skip)} already-existing field(s): "
                + ", ".join(to_skip)
            )

        if not to_create:
            self.conf.logger.info("All requested custom fields already exist — nothing to create.")
            return

        created = 0
        failed = 0
        for label in to_create:
            if self._create_one(label):
                self.conf.logger.info(f"  Created custom field: {label} ({_FIELD_TYPES.get(label, 'DROPDOWN')})")
                created += 1
            else:
                failed += 1

        self.conf.logger.info(
            f"Custom field creation complete: {created} created, "
            f"{len(to_skip)} skipped (already existed), {failed} failed."
        )
        if failed:
            sys.exit(1)

        # Explicitly POST options for every DROPDOWN field.
        # BD may silently ignore ``initialOptions`` on creation, so we always
        # push options separately to guarantee they exist.
        self.ensure_dropdown_options()

    def get_field_id_map(self) -> dict[str, str]:
        """
        Return a dict mapping every SC-* custom field label to its field ID.

        The field ID is the last path segment of the field definition's _meta.href,
        e.g. ``https://…/custom-fields/objects/component/fields/9`` → ``"9"``.
        """
        url = self._get_fields_url()
        res = self.bd.get_json(url, headers={"accept": _ADMIN_ACCEPT})

        field_map: dict[str, str] = {}
        for item in res.get("items", []):
            label = item.get("label", "")
            if not label.startswith("SC-"):
                continue
            href = item.get("_meta", {}).get("href", "")
            field_id = href.rstrip("/").split("/")[-1]
            if field_id:
                field_map[label] = field_id
        return field_map

    def build_option_href_map(
        self, field_id_map: dict[str, str]
    ) -> dict[str, dict[str, str]]:
        """
        For each SC-* DROPDOWN field, fetch its options and return a map of
        ``{field_label: {option_label: option_href}}``.

        BD requires the full ``_meta.href`` of the selected option in the
        ``values`` array when PUTting a DROPDOWN field value on a component.
        """
        url = self._get_fields_url()
        res = self.bd.get_json(url, headers={"accept": _ADMIN_ACCEPT})

        option_href_map: dict[str, dict[str, str]] = {}
        for item in res.get("items", []):
            label = item.get("label", "")
            if label not in field_id_map or item.get("type") != "DROPDOWN":
                continue
            href = item.get("_meta", {}).get("href", "")
            field_id = href.rstrip("/").split("/")[-1]
            if not field_id:
                continue
            options = self._fetch_field_options(field_id)
            label_to_href: dict[str, str] = {}
            for opt in options:
                opt_label = opt.get("label", "")
                opt_href = opt.get("_meta", {}).get("href", "")
                if opt_label and opt_href:
                    label_to_href[opt_label] = opt_href
            if label_to_href:
                option_href_map[label] = label_to_href
                self.conf.logger.debug(
                    f"  {label}: mapped {len(label_to_href)} option href(s)"
                )
        return option_href_map

    def get_component_sc_date(self, component_url: str, sc_date_field_id: str) -> datetime | None:
        """
        Read the current SC-Date custom field value from a component.

        Returns a timezone-aware datetime if a valid value exists, or None if
        the field is unset or the value cannot be parsed.
        """
        component_id = component_url.rstrip("/").split("/")[-1]
        url = f"{self.bd.base_url}/api/components/{component_id}/custom-fields/{sc_date_field_id}"
        try:
            res = self.bd.get_json(url, headers={"accept": _COMP_CONTENT_TYPE})
            values = res.get("values", [])
            if not values:
                return None
            date_str = values[0]
            # BD returns dates as "2026-03-02T00:00:00.000Z"
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception as exc:
            self.conf.logger.debug(
                f"  Could not read SC-Date for component {component_id}: {exc}"
            )
            return None

    def get_sc_date_map(
        self,
        comp_urls: list[str],
        sc_date_field_id: str,
        workers: int = 8,
    ) -> dict[str, datetime | None]:
        """
        Fetch the SC-Date custom field for multiple components in parallel.

        Returns ``{component_url: datetime_or_None}``.
        """
        results: dict[str, datetime | None] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_url = {
                pool.submit(self.get_component_sc_date, url, sc_date_field_id): url
                for url in comp_urls
            }
            for future in as_completed(future_to_url):
                comp_url = future_to_url[future]
                try:
                    results[comp_url] = future.result()
                except Exception as exc:
                    self.conf.logger.debug(
                        f"  SC-Date check failed for {comp_url.split('/')[-1]}: {exc}"
                    )
                    results[comp_url] = None
        return results

    def get_component_sc_sourcerepo(self, component_url: str, field_id: str) -> str | None:
        """Read the current SC-Sourcerepo custom field value from a component."""
        component_id = component_url.rstrip("/").split("/")[-1]
        url = f"{self.bd.base_url}/api/components/{component_id}/custom-fields/{field_id}"
        try:
            res = self.bd.get_json(url, headers={"accept": _COMP_CONTENT_TYPE})
            values = res.get("values", [])
            return values[0] if values else None
        except Exception as exc:
            self.conf.logger.debug(
                f"  Could not read SC-Sourcerepo for component {component_id}: {exc}"
            )
            return None

    def get_sc_sourcerepo_map(
        self,
        comp_urls: list[str],
        field_id: str,
        workers: int = 8,
    ) -> dict[str, str | None]:
        """
        Fetch the SC-Sourcerepo custom field for multiple components in parallel.

        Returns ``{component_url: repo_url_or_None}``.
        """
        results: dict[str, str | None] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_url = {
                pool.submit(self.get_component_sc_sourcerepo, url, field_id): url
                for url in comp_urls
            }
            for future in as_completed(future_to_url):
                comp_url = future_to_url[future]
                try:
                    results[comp_url] = future.result()
                except Exception as exc:
                    self.conf.logger.debug(
                        f"  SC-Sourcerepo check failed for {comp_url.split('/')[-1]}: {exc}"
                    )
                    results[comp_url] = None
        return results

    def upload_to_component(
        self,
        component_url: str,
        scorecard_data: dict,
        field_id_map: dict[str, str],
        option_href_map: dict[str, dict[str, str]] | None = None,
        repo_url: str | None = None,
    ) -> tuple[int, int]:
        """
        Write scorecard values to every matching SC-* custom field on a component.

        ``option_href_map`` should be the result of ``build_option_href_map()``.
        For DROPDOWN fields the value placed in the ``values`` array is the full
        ``_meta.href`` of the matching option (as required by the BD API).

        Returns ``(fields_set, fields_skipped)`` counts.
        """
        component_id = component_url.rstrip("/").split("/")[-1]
        option_href_map = option_href_map or {}

        def _dropdown_href(field_label: str, score: float) -> str | None:
            """Return the option href for a score on a DROPDOWN field, or None."""
            opt_label = _score_to_label(score)
            if opt_label is None:
                return None
            return option_href_map.get(field_label, {}).get(opt_label)

        # Build label → value map from scorecard data
        values: dict[str, str] = {}

        # SC-Overall
        overall_score = scorecard_data.get("score", -1)
        if SC_OVERALL in field_id_map:
            href = _dropdown_href(SC_OVERALL, overall_score)
            if href:
                values[SC_OVERALL] = href

        # SC-Date: full ISO-8601 datetime string
        if SC_DATE in field_id_map:
            v = _date_to_bd(scorecard_data.get("date", ""))
            if v is not None:
                values[SC_DATE] = v

        # SC-Sourcerepo: plain URL string (TEXT field)
        if repo_url and SC_SOURCEREPO in field_id_map:
            values[SC_SOURCEREPO] = repo_url

        # Individual checks: "SC-" + check_name
        for check in scorecard_data.get("checks", []):
            label = f"SC-{check.get('name', '')}"
            if label not in field_id_map:
                continue
            href = _dropdown_href(label, check.get("score", -1))
            if href:
                values[label] = href

        # Upload
        set_count = skipped_count = 0
        for label, value in values.items():
            field_id = field_id_map[label]
            if self._put_component_field(component_id, field_id, value, label):
                set_count += 1
            else:
                skipped_count += 1

        return set_count, skipped_count

    def upload_components(
        self,
        comp_scorecard: dict[str, dict],
        field_id_map: dict[str, str],
        option_href_map: dict[str, dict[str, str]] | None = None,
        workers: int = 8,
        comp_repo_urls: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        """
        Upload scorecard data to multiple components in parallel.

        Returns ``(total_fields_set, total_fields_failed)`` across all components.
        """
        total_set = total_skipped = 0
        total = len(comp_scorecard)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            _repo_urls = comp_repo_urls or {}
            future_to_url = {
                pool.submit(
                    self.upload_to_component,
                    comp_url, sc_data, field_id_map, option_href_map,
                    _repo_urls.get(comp_url),
                ): comp_url
                for comp_url, sc_data in comp_scorecard.items()
            }
            for future in as_completed(future_to_url):
                comp_url = future_to_url[future]
                try:
                    set_count, skip_count = future.result()
                except Exception as exc:
                    self.conf.logger.warning(
                        f"  Upload failed for {comp_url.split('/')[-1]}: {exc}"
                    )
                    set_count, skip_count = 0, 1
                total_set     += set_count
                total_skipped += skip_count
                self.conf.logger.debug(
                    f"  [{total_set + total_skipped}/{total}] "
                    f"{comp_url.split('/')[-1]} — "
                    f"{set_count} field(s) set, {skip_count} failed"
                )
        return total_set, total_skipped
