import pathlib
import sys
from datetime import datetime, timedelta, timezone

# Ensure the project root (parent of this package) is on sys.path so that
# pkg_repo_lookup.py (imported by ComponentListClass) can be found whether the
# user runs via the installed bd-scorecard-lookup command or via `python -m bd_scorecard`.
_PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from blackduck import Client

from .BOMClass import BOM
from .ConfigClass import Config
from .CustomFieldsClass import CustomFields, SC_DATE, SC_SOURCEREPO


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
    sc_date_map: dict[str, datetime | None] = {}
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=conf.update_period)
        if conf.update_period > 0 else None
    )
    if sc_date_field_id and cutoff is not None and supported_pkg_ids:
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
    # 2c. Read cached SC-Sourcerepo values to skip pkg→repo resolution
    # ------------------------------------------------------------------ #
    pkg_id_cached_repo: dict[str, str] = {}
    sc_sourcerepo_field_id = field_id_map.get(SC_SOURCEREPO)
    if sc_sourcerepo_field_id and lookup_pkg_ids:
        lookup_comp_urls: set[str] = {
            comp.data.get('component', '')
            for pid in lookup_pkg_ids
            if (comp := pkg_id_map.get(pid)) and comp.data.get('component', '')
        }
        conf.logger.info(
            f"Reading cached SC-Sourcerepo for {len(lookup_comp_urls)} component(s) …"
        )
        cached_repo_map = cf.get_sc_sourcerepo_map(
            list(lookup_comp_urls),
            sc_sourcerepo_field_id,
            workers=conf.workers,
        )
        for pid in lookup_pkg_ids:
            comp = pkg_id_map.get(pid)
            if comp:
                repo_url = cached_repo_map.get(comp.data.get('component', ''))
                if repo_url:
                    pkg_id_cached_repo[pid] = repo_url
        if pkg_id_cached_repo:
            conf.logger.info(
                f"Cached SC-Sourcerepo found for {len(pkg_id_cached_repo)} package(s) — "
                f"skipping pkg→repo resolution for these"
            )

    # ------------------------------------------------------------------ #
    # 3. Look up scorecard data for packages that need updating
    # ------------------------------------------------------------------ #
    scorecard_results: dict = {}
    if lookup_pkg_ids:
        conf.logger.info(
            f"Querying scorecard data for {len(lookup_pkg_ids)} package(s) …"
        )
        try:
            scorecard_results = complist.lookup_scorecard(
                lookup_pkg_ids,
                workers=conf.workers,
                on_progress=conf.logger.info,
                pre_resolved=pkg_id_cached_repo,
            )
        except Exception as exc:
            conf.logger.error(f"Scorecard lookup failed: {exc}")
            sys.exit(-1)

    hits = sum(1 for e in scorecard_results.values() if e.get('scorecard') is not None)
    conf.logger.info(
        f"Scorecard data found for {hits} / {len(lookup_pkg_ids)} looked-up package(s)"
    )

    # ------------------------------------------------------------------ #
    # 4. Upload scorecard values to Component custom fields in Black Duck
    # ------------------------------------------------------------------ #
    # Verify DROPDOWN field options and build the option href map in one fetch.
    option_href_map = cf.prepare_for_upload(field_id_map)

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
    comp_repo_urls: dict[str, str] = {}   # comp_url → resolved repo URL (for SC-Sourcerepo)
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
            # Only stamp SC-Date if it was blank or older than update_period.
            # When update_period=0 (force-refresh), cutoff is None → always stamp.
            existing = sc_date_map.get(comp_url)
            if cutoff is None or existing is None or existing < cutoff:
                comp_date_only[comp_url] = today
        # Collect repo URLs for SC-Sourcerepo upload (both match and no-match).
        if pkg_id in lookup_set:
            repo_url = scorecard_results.get(pkg_id, {}).get('repo_url', '')
            if repo_url:
                comp_repo_urls[comp_url] = repo_url

    if not comp_scorecard and not comp_date_only:
        conf.logger.info("No components to update — nothing to upload.")
        return

    total_set = total_skipped = 0

    if comp_scorecard:
        conf.logger.info(
            f"Uploading scorecard data to {len(comp_scorecard)} component(s) …"
        )
        s, f = cf.upload_components(
            comp_scorecard, field_id_map, option_href_map,
            workers=conf.workers, comp_repo_urls=comp_repo_urls,
        )
        total_set += s
        total_skipped += f

    if comp_date_only:
        conf.logger.info(
            f"Setting SC-Date to today for {len(comp_date_only)} component(s) with no scorecard data …"
        )
        s, f = cf.upload_components(
            comp_date_only, field_id_map, option_href_map,
            workers=conf.workers, comp_repo_urls=comp_repo_urls,
        )
        total_set += s
        total_skipped += f

    conf.logger.info(
        f"Upload complete: {total_set} field value(s) written, "
        f"{total_skipped} failed across {len(comp_scorecard) + len(comp_date_only)} component(s)."
    )

    # ------------------------------------------------------------------ #
    # 5. Optional report
    # ------------------------------------------------------------------ #
    if conf.report:
        _write_report(conf, pkg_id_map, scorecard_results, lookup_set)


def _write_report(conf, pkg_id_map, scorecard_results, lookup_set):
    lines = []
    matched = []
    unmatched = []
    skipped = []

    seen: set[str] = set()
    for pkg_id, comp in pkg_id_map.items():
        comp_url = comp.data.get('component', '')
        if comp_url in seen:
            continue
        seen.add(comp_url)

        if pkg_id not in lookup_set:
            skipped.append((pkg_id, comp))
        elif scorecard_results.get(pkg_id, {}).get('scorecard') is not None:
            matched.append((pkg_id, comp, scorecard_results[pkg_id]['scorecard']))
        else:
            unmatched.append((pkg_id, comp))

    lines.append("=" * 72)
    lines.append("OpenSSF Scorecard Report")
    lines.append(f"Project : {conf.bd_project}  Version : {conf.bd_version}")
    lines.append(f"Date    : {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    lines.append("=" * 72)

    lines.append(f"\n--- Components with Scorecard data ({len(matched)}) ---\n")
    for pkg_id, comp, sc in sorted(matched, key=lambda x: x[0]):
        overall = sc.get('score', 'n/a')
        repo = sc.get('repo', {}).get('name', '')
        lines.append(f"  {comp.name} {comp.version}")
        lines.append(f"    pkg_id  : {pkg_id}")
        lines.append(f"    repo    : {repo}")
        lines.append(f"    score   : {overall}")
        checks = sc.get('checks', [])
        if checks:
            lines.append("    checks  :")
            for chk in sorted(checks, key=lambda c: c.get('name', '')):
                lines.append(f"      {chk.get('name',''):<30} {chk.get('score', 'n/a')}")
        lines.append("")

    lines.append(f"--- Components with no Scorecard data ({len(unmatched)}) ---\n")
    for pkg_id, comp in sorted(unmatched, key=lambda x: x[0]):
        entry = scorecard_results.get(pkg_id, {})
        err = entry.get('error', '')
        lines.append(f"  {comp.name} {comp.version}  [{pkg_id}]")
        if err:
            lines.append(f"    reason : {err}")
        lines.append("")

    if skipped:
        lines.append(f"--- Components skipped (SC-Date within {conf.update_period} days) ({len(skipped)}) ---\n")
        for pkg_id, comp in sorted(skipped, key=lambda x: x[0]):
            lines.append(f"  {comp.name} {comp.version}  [{pkg_id}]")
        lines.append("")

    lines.append("=" * 72)

    try:
        with open(conf.report, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        conf.logger.info(f"Report written to {conf.report}")
    except OSError as exc:
        conf.logger.error(f"Failed to write report: {exc}")


if __name__ == '__main__':
    main()
