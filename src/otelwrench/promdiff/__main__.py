import sys

from otelwrench.cli import main

raise SystemExit(main(["promdiff", *sys.argv[1:]]))
