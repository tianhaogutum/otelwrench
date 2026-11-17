import sys

from otelwrench.cli import main

raise SystemExit(main(["privacy", *sys.argv[1:]]))
