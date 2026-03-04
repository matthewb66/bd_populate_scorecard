import argparse
import logging
import os

from .CustomFieldsClass import VALID_SC_FIELDS


class Config:
    def __init__(self):
        self.bd_api = ''
        self.bd_url = ''
        self.bd_trustcert = False

        self.bd_project = ''
        self.bd_version = ''
        self.workers = 8
        self.output = ''
        self.compact = False
        self.debug = False
        self.logfile = ''
        self.logger = None
        # None  → option not supplied (normal run)
        # list  → create_custom_fields was supplied (list may be empty → SC-Overall only)
        self.create_custom_fields: list[str] | None = None
        self.update_period: int = 30

    def get_cli_args(self):
        parser = argparse.ArgumentParser(
            description='Look up OpenSSF Scorecard scores for components in a Black Duck BOM',
            prog='bd_scorecard',
        )

        parser.add_argument('--blackduck_url', type=str,
                            help='Black Duck server URL (or set BLACKDUCK_URL)', default='')
        parser.add_argument('--blackduck_api_token', type=str,
                            help='Black Duck API token (or set BLACKDUCK_API_TOKEN)', default='')
        parser.add_argument('--blackduck_trust_cert',
                            help='Trust the Black Duck server TLS certificate', action='store_true')
        parser.add_argument('-p', '--project',
                            help='Black Duck project name (REQUIRED)', default='')
        parser.add_argument('-v', '--version',
                            help='Black Duck project version name (REQUIRED)', default='')
        parser.add_argument('--workers', type=int, default=8, metavar='N',
                            help='Parallel workers for Scorecard API requests (default: 8)')
        parser.add_argument('-o', '--output', metavar='FILE',
                            help='Write JSON output to FILE (default: stdout)', default='')
        parser.add_argument('--compact', action='store_true',
                            help='Emit compact single-line JSON instead of pretty-printed')
        parser.add_argument(
            '--create_custom_fields',
            nargs='?',
            const='',
            default=None,
            metavar='FIELD_LIST',
            help=(
                'Create Component-level custom fields and exit. '
                '-p / -v are not required with this option. '
                'FIELD_LIST is an optional comma-delimited subset of: '
                + ', '.join(VALID_SC_FIELDS) + '. '
                'SC-Overall and SC-Date are always created. '
                'Omit FIELD_LIST (or pass an empty string) to create SC-Overall and SC-Date only.'
            ),
        )
        parser.add_argument('--update_period', type=int, default=30, metavar='DD',
                            help='Only upload scorecard data newer than DD days (default: 30)')
        parser.add_argument('--debug', help='Enable debug logging', action='store_true')
        parser.add_argument('--logfile', help='Write log output to FILE', default='')

        args = parser.parse_args()

        loglevel = logging.DEBUG if args.debug else logging.INFO
        self.logfile = args.logfile
        self.logger = self.setup_logger('bd-scorecard', loglevel)

        self.logger.debug("ARGUMENTS:")
        for arg in vars(args):
            self.logger.debug(f"  --{arg}={getattr(args, arg)}")

        terminate = False

        # Black Duck URL
        url = os.environ.get('BLACKDUCK_URL')
        if args.blackduck_url:
            self.bd_url = args.blackduck_url
        elif url:
            self.bd_url = url
        else:
            self.logger.error("Black Duck URL not specified (--blackduck_url or BLACKDUCK_URL)")
            terminate = True

        # Black Duck API token
        api = os.environ.get('BLACKDUCK_API_TOKEN')
        if args.blackduck_api_token:
            self.bd_api = args.blackduck_api_token
        elif api:
            self.bd_api = api
        else:
            self.logger.error("Black Duck API token not specified (--blackduck_api_token or BLACKDUCK_API_TOKEN)")
            terminate = True

        # Trust cert
        trustcert = os.environ.get('BLACKDUCK_TRUST_CERT')
        if trustcert == 'true' or args.blackduck_trust_cert:
            self.bd_trustcert = True

        # Project / version — only required for normal runs, not for --create_custom_fields
        if args.project and args.version:
            self.bd_project = args.project
            self.bd_version = args.version
        elif args.create_custom_fields is None:
            self.logger.error("Black Duck project and version are required (-p / -v)")
            terminate = True

        self.workers = args.workers
        self.output = args.output
        self.compact = args.compact
        self.debug = args.debug
        self.update_period = args.update_period

        # --create_custom_fields validation
        if args.create_custom_fields is not None:
            raw_list = args.create_custom_fields
            if raw_list.strip():
                requested = [f.strip() for f in raw_list.split(',') if f.strip()]
                invalid = [f for f in requested if f not in VALID_SC_FIELDS]
                if invalid:
                    self.logger.error(
                        "Unknown field name(s) for --create_custom_fields: "
                        + ', '.join(invalid)
                        + f". Valid names are: {', '.join(VALID_SC_FIELDS)}"
                    )
                    terminate = True
                else:
                    self.create_custom_fields = requested
            else:
                self.create_custom_fields = []   # empty → SC-Overall and SC-Date only

        if terminate:
            return False
        return True

    def setup_logger(self, name: str, level) -> logging.Logger:
        logger = logging.getLogger(name)
        logger.setLevel(level)

        if not logger.hasHandlers():
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

            if self.logfile:
                file_handler = logging.FileHandler(self.logfile)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

        return logger
