import sys

from otelwrench.cli import main

raise SystemExit(main(["spanmetrics", *sys.argv[1:]]))
