import pathlib
import sys
from datetime import datetime, timedelta, timezone

# Ensure the project root (parent of this package) is on sys.path so that
# scorecard_lookup.py can be imported whether the user runs via the top-level
# bd_scorecard_lookup.py entry point or via `python -m bd_scorecard`.
_PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import scorecard_lookup  # noqa: E402 (must come after path manipulation)

from blackduck import Client

from .BOMClass import BOM
from .ConfigClass import Config
from .CustomFieldsClass import CustomFields, SC_DATE


def main():
    conf = Config()
    if not conf.get_cli_args():
        sys.exit(2)

    process(conf)
    sys.exit(0)


def _make_bd_client(conf) -> Client:
    return Client(
        token=conf.bd_api,
        base_url=conf.bd_url,
        verify=(not conf.bd_trustcert),
        timeout=60,
    )


def process(conf):
    # ------------------------------------------------------------------ #
    # 1a. --create_custom_fields: create fields then exit
    # ------------------------------------------------------------------ #
    if conf.create_custom_fields is not None:
        conf.logger.info(
            f"Connecting to {conf.bd_url} to manage custom fields …"
        )
        bd = _make_bd_client(conf)
        cf = CustomFields(bd, conf)
        cf.create_fields(conf.create_custom_fields)
        return

    # ------------------------------------------------------------------ #
    # 1b. Normal run: verify SC-* custom fields exist before doing any work
    # ------------------------------------------------------------------ #
    bd = _make_bd_client(conf)
    cf = CustomFields(bd, conf)

    conf.logger.info("Checking SC-* Component custom fields in Black Duck …")
    field_id_map = cf.get_field_id_map()

    if not field_id_map:
        conf.logger.error(
            "No SC-* custom fields found on the Component object. "
            "Run with --create_custom_fields to create them first."
        )
        sys.exit(1)

    conf.logger.info(
        f"Found {len(field_id_map)} SC-* field(s): "
        + ", ".join(sorted(field_id_map))
    )

    # ------------------------------------------------------------------ #
    # 1c. Connect to Black Duck and fetch the BOM
    # ------------------------------------------------------------------ #
    bom = BOM(conf)
    complist = bom.get_components()

    conf.logger.info(f"Total resolved BOM components: {complist.count()}")

    # ------------------------------------------------------------------ #
    # 2. Build the pkg_id → Component map for supported ecosystems
    # ------------------------------------------------------------------ #
    pkg_id_map = complist.get_pkg_id_map()   # { "npm:lodash": Component, ... }
    supported_pkg_ids = list(pkg_id_map.keys())

    conf.logger.info(
        f"Components with supported package manager origins: {len(supported_pkg_ids)} "
        f"({complist.count() - len(complist.get_unsupported())} components)"
    )

    # ------------------------------------------------------------------ #
    # 2b. Skip components whose existing SC-Date is already fresh
    # ------------------------------------------------------------------ #
    # Read the current SC-Date from Black Duck for each unique component.
    # Components whose date is within --update_period days are excluded from
    # the scorecard.dev lookup, reducing unnecessary API traffic.
    lookup_pkg_ids = supported_pkg_ids
    sc_date_field_id = field_id_map.get(SC_DATE)
    if sc_date_field_id and conf.update_period > 0 and supported_pkg_ids:
        cutoff = datetime.now(timezone.utc) - timedelta(days=conf.update_period)
        conf.logger.info(
            f"Checking existing SC-Date values (cutoff: {cutoff.date()}, "
            f"--update_period={conf.update_period}) …"
        )
        # Build comp_url → [pkg_ids] so each component is checked only once
        comp_url_to_pkg_ids: dict[str, list[str]] = {}
        for pkg_id, comp in pkg_id_map.items():
            comp_url = comp.data.get('component', '')
            if comp_url:
                comp_url_to_pkg_ids.setdefault(comp_url, []).append(pkg_id)

        sc_date_map = cf.get_sc_date_map(
            list(comp_url_to_pkg_ids.keys()),
            sc_date_field_id,
            workers=conf.workers,
        )
        fresh_pkg_ids: set[str] = set()
        for comp_url, pkg_ids in comp_url_to_pkg_ids.items():
            sc_date = sc_date_map.get(comp_url)
            if sc_date and sc_date >= cutoff:
                fresh_pkg_ids.update(pkg_ids)
                conf.logger.debug(
                    f"  {pkg_ids[0]}: SC-Date {sc_date.date()} is within "
                    f"{conf.update_period} days — skipping lookup"
                )

        if fresh_pkg_ids:
            conf.logger.info(
                f"Skipping {len(fresh_pkg_ids)} package(s) with fresh SC-Date "
                f"(within {conf.update_period} days)"
            )
        lookup_pkg_ids = [p for p in supported_pkg_ids if p not in fresh_pkg_ids]

    # ------------------------------------------------------------------ #
    # 3. Look up scorecard data for packages that need updating
    # ------------------------------------------------------------------ #
    scorecard_results: dict = {}
    if lookup_pkg_ids:
        conf.logger.info(
            f"Querying scorecard data for {len(lookup_pkg_ids)} package(s) …"
        )
        try:
            scorecard_results = scorecard_lookup.run(
                lookup_pkg_ids,
                workers=conf.workers,
                on_progress=conf.logger.info,
            )
        except Exception as exc:
            conf.logger.error(f"Scorecard lookup failed: {exc}")
            sys.exit(-1)

    hits = sum(1 for e in scorecard_results.values() if e.get('scorecard') is not None)
    conf.logger.info(
        f"Scorecard data found for {hits} / {len(supported_pkg_ids)} supported package(s)"
    )

    # ------------------------------------------------------------------ #
    # 4. Upload scorecard values to Component custom fields in Black Duck
    # ------------------------------------------------------------------ #
    # Ensure every DROPDOWN field has its options (0–10) before writing values,
    # then resolve option labels to BD's internal option IDs.
    cf.ensure_dropdown_options()
    option_href_map = cf.build_option_href_map(field_id_map)

    conf.logger.info(
        f"Uploading scorecard data to {len(field_id_map)} field(s) …"
    )

    # Build two dicts keyed by component_url (deduplicated across multiple origins):
    #   comp_scorecard  — components with full scorecard data to upload
    #   comp_date_only  — looked-up components with no scorecard data; SC-Date
    #                     is set to today so they won't be re-fetched before
    #                     --update_period days have elapsed
    lookup_set = set(lookup_pkg_ids)
    today = {'date': datetime.now(timezone.utc).strftime('%Y-%m-%d')}
    comp_scorecard: dict[str, dict] = {}
    comp_date_only: dict[str, dict] = {}
    seen_comp_urls: set[str] = set()
    for pkg_id, comp in pkg_id_map.items():
        comp_url = comp.data.get('component', '')
        if not comp_url or comp_url in seen_comp_urls:
            continue
        seen_comp_urls.add(comp_url)
        sc_data = scorecard_results.get(pkg_id, {}).get('scorecard')
        if sc_data:
            comp_scorecard[comp_url] = sc_data
        elif pkg_id in lookup_set and SC_DATE in field_id_map:
            comp_date_only[comp_url] = today

    if not comp_scorecard and not comp_date_only:
        conf.logger.info("No components to update — nothing to upload.")
        return

    total_set = total_skipped = 0

    if comp_scorecard:
        conf.logger.info(
            f"Uploading scorecard data to {len(comp_scorecard)} component(s) …"
        )
        s, f = cf.upload_components(comp_scorecard, field_id_map, option_href_map, workers=conf.workers)
        total_set += s
        total_skipped += f

    if comp_date_only:
        conf.logger.info(
            f"Setting SC-Date to today for {len(comp_date_only)} component(s) with no scorecard data …"
        )
        s, f = cf.upload_components(comp_date_only, field_id_map, option_href_map, workers=conf.workers)
        total_set += s
        total_skipped += f

    conf.logger.info(
        f"Upload complete: {total_set} field value(s) written, "
        f"{total_skipped} failed across {len(comp_scorecard) + len(comp_date_only)} component(s)."
    )


if __name__ == '__main__':
    main()
