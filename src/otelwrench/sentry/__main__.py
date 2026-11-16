import sys

from otelwrench.cli import main

raise SystemExit(main(["sentry", *sys.argv[1:]]))
