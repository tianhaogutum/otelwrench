import sys

from otelwrench.cli import main

raise SystemExit(main(["tailsim", *sys.argv[1:]]))
