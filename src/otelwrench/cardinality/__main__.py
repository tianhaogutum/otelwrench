import sys

from otelwrench.cli import main

raise SystemExit(main(["cardinality", *sys.argv[1:]]))
