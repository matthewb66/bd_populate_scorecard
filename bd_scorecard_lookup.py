#!/usr/bin/env python3
"""
Entry point for bd_scorecard — look up OpenSSF Scorecard data for every
package-manager component in a Black Duck project BOM.

Usage:
    python bd_scorecard_lookup.py \\
        --blackduck_url https://sca247.poc.blackduck.com \\
        --blackduck_api_token <TOKEN> \\
        -p Demo-insecure-bank-nodejs -v 0.0.0 \\
        --output results.json

Environment variables (alternatives to CLI flags):
    BLACKDUCK_URL
    BLACKDUCK_API_TOKEN
    BLACKDUCK_TRUST_CERT=true
"""

from bd_scorecard.main import main

if __name__ == '__main__':
    main()
