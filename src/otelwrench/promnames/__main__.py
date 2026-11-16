import sys

from otelwrench.cli import main

raise SystemExit(main(["prom-names", *sys.argv[1:]]))
