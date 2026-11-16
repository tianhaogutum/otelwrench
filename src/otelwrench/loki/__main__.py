import sys

from otelwrench.cli import main

raise SystemExit(main(["loki", *sys.argv[1:]]))
