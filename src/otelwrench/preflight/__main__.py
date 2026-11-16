import sys

from otelwrench.cli import main

raise SystemExit(main(["preflight", *sys.argv[1:]]))
