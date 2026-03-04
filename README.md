# bd_scorecard_lookup

Fetches [OpenSSF Scorecard](https://securityscorecards.dev) data for every package-manager component in a Black Duck project BOM and writes the results back to Component-level custom fields in Black Duck.

## How it works

1. Reads the resolved BOM from a Black Duck project version.
2. Checks each component's existing `SC-Date` custom field — components updated within `--update_period` days are skipped to avoid unnecessary API calls.
3. Resolves the remaining packages to their source repositories (npm, PyPI, RubyGems, NuGet) and queries the public [Scorecard REST API](https://api.securityscorecards.dev) in parallel.
4. Writes a JSON report to stdout or a file.
5. Uploads the scorecard scores to Black Duck Component custom fields (`SC-Overall`, `SC-Date`, and any individual check fields you have created).  Components with no scorecard data still get `SC-Date` set to today so they are not re-queried until the next update period.

Supported ecosystems: **npm**, **PyPI**, **RubyGems**, **NuGet**.

---

## Requirements

- Python 3.10+
- A Black Duck instance with API access
- No external credentials are required for the Scorecard API

Install the package and its dependencies:

```
pip install .
```

For development, install in editable mode so changes to the source take effect immediately:

```
pip install -e .
```

Once installed, the `bd-scorecard-lookup` command is available on your PATH.

---

## Setup — create custom fields (first run only)

Before running the main script, create the `SC-*` Component custom fields in Black Duck.  You only need to do this once.

**Create SC-Overall and SC-Date only:**

```
bd-scorecard-lookup \
    --blackduck_url https://your-blackduck-server \
    --blackduck_api_token <TOKEN> \
    --create_custom_fields
```

**Create SC-Overall, SC-Date, and all individual check fields:**

```
bd-scorecard-lookup \
    --blackduck_url https://your-blackduck-server \
    --blackduck_api_token <TOKEN> \
    --create_custom_fields "SC-Maintained,SC-Code-Review,SC-Dangerous-Workflow,SC-Binary-Artifacts,SC-Token-Permissions,SC-CII-Best-Practices,SC-Security-Policy,SC-License,SC-Fuzzing,SC-Signed-Releases,SC-Branch-Protection,SC-Packaging,SC-Pinned-Dependencies,SC-SAST"
```

Available individual check fields:

| Field name | Description |
|---|---|
| `SC-Maintained` | Is the project actively maintained? |
| `SC-Dangerous-Workflow` | Are dangerous GitHub Actions workflow patterns avoided? |
| `SC-Code-Review` | Are code changes reviewed before merging? |
| `SC-Binary-Artifacts` | Does the project avoid binary artifacts in its source? |
| `SC-Token-Permissions` | Does the project use minimal GitHub token permissions? |
| `SC-CII-Best-Practices` | Has the project earned an OpenSSF Best Practices Badge? |
| `SC-Security-Policy` | Has the project published a security policy? |
| `SC-License` | Does the project declare a license? |
| `SC-Fuzzing` | Does the project use fuzzing tools? |
| `SC-Signed-Releases` | Does the project cryptographically sign releases? |
| `SC-Branch-Protection` | Does the project use branch protection rules? |
| `SC-Packaging` | Does the project build and publish official packages? |
| `SC-Pinned-Dependencies` | Are the project's dependencies pinned to specific versions? |
| `SC-SAST` | Does the project use static application security testing? |

Only fields that exist in Black Duck will be written to; others are silently skipped.

---

## Usage

```
bd-scorecard-lookup \
    --blackduck_url https://your-blackduck-server \
    --blackduck_api_token <TOKEN> \
    -p "My Project" \
    -v "1.0.0"
```

### All options

| Flag | Default | Description |
|---|---|---|
| `--blackduck_url` | `BLACKDUCK_URL` env var | Black Duck server URL |
| `--blackduck_api_token` | `BLACKDUCK_API_TOKEN` env var | Black Duck API token |
| `--blackduck_trust_cert` | off | Trust the server's TLS certificate (self-signed) |
| `-p`, `--project` | — | Black Duck project name **(required)** |
| `-v`, `--version` | — | Black Duck project version name **(required)** |
| `--update_period DD` | `30` | Skip components whose `SC-Date` is within DD days; set to `0` to always refresh |
| `--workers N` | `8` | Number of parallel threads for API requests |
| `--create_custom_fields [FIELD_LIST]` | — | Create custom fields then exit (see Setup above) |
| `--debug` | off | Enable debug-level logging |
| `--logfile FILE` | — | Write log output to FILE in addition to stderr |

### Environment variables

Credentials can be supplied via environment variables instead of CLI flags:

```
export BLACKDUCK_URL=https://your-blackduck-server
export BLACKDUCK_API_TOKEN=<TOKEN>
export BLACKDUCK_TRUST_CERT=true   # optional
```

---

## Examples

**Force refresh all components:**

```
bd-scorecard-lookup \
    --blackduck_url https://your-blackduck-server \
    --blackduck_api_token <TOKEN> \
    -p "My Project" -v "1.0.0" \
    --update_period 0
```

**Use 16 parallel workers and enable debug logging:**

```
bd-scorecard-lookup \
    --blackduck_url https://your-blackduck-server \
    --blackduck_api_token <TOKEN> \
    -p "My Project" -v "1.0.0" \
    --workers 16 \
    --debug \
    --logfile run.log
```

**Run as a scheduled job (cron, CI pipeline) — refresh every 30 days:**

```
bd-scorecard-lookup \
    --blackduck_url "$BLACKDUCK_URL" \
    --blackduck_api_token "$BLACKDUCK_API_TOKEN" \
    -p "My Project" -v "1.0.0" \
    --update_period 30
```
