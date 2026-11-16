"""Read local files and print a report. No execution, network, or deployment."""

import argparse
import json
import sys

from . import __version__
from .arrivals import compare_arrivals, load_arrivals, replay_arrivals
from .corpus import load_corpus
from .evidence import (load_arrival_comparison_evidence, load_arrival_evidence,
                       load_comparison_evidence, load_replay_evidence,
                       verify_arrival_comparison_evidence, verify_arrival_evidence,
                       verify_comparison_evidence, verify_replay_evidence)
from .policy import load_config
from .provenance import (load_arrival_comparison_fingerprints, load_arrival_fingerprints,
                         load_comparison_fingerprints, load_replay_fingerprints,
                         verify_arrival_comparison, verify_arrivals, verify_comparison,
                         verify_replay)
from .replay import compare, replay
from .validation import InputError


class _DiagnosticLimit(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error('--diagnostic-limit may appear only once')
        if not value.isascii() or not value.isdecimal():
            parser.error('--diagnostic-limit must be a decimal integer from 1 to 1000')
        # Compare lengths before int conversion, including very large arguments.
        digits = value.lstrip('0')
        if not digits or len(digits) > 4 or int(digits) > 1000:
            parser.error('--diagnostic-limit must be a decimal integer from 1 to 1000')
        setattr(namespace, self.dest, int(digits))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='otelwrench tailsim', description='Offline policy replay on OTLP JSON traces')
    parser.add_argument('--version', action='version', version=f'otelwrench tailsim {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    single = commands.add_parser('replay', help='explain each complete-trace decision')
    single.add_argument('corpus', help='OTLP/HTTP ExportTraceServiceRequest JSON file')
    single.add_argument('--config', required=True)
    verify_static = commands.add_parser('verify-replay', help='check saved static replay input fingerprints')
    verify_static.add_argument('corpus', help='supported OTLP JSON corpus')
    verify_static.add_argument('--report', required=True, help='saved schema-1 static replay JSON report')
    verify_static.add_argument('--config', required=True, help='supported policy-only configuration')
    evidence = commands.add_parser('verify-replay-evidence', allow_abbrev=False,
                                   help='compare saved static evidence with local replay')
    evidence.add_argument('corpus', help='supported OTLP JSON corpus')
    evidence.add_argument('--report', required=True, help='saved schema-1 static replay JSON report')
    evidence.add_argument('--config', required=True, help='supported policy-only configuration')
    evidence.add_argument('--diagnostic-limit', action=_DiagnosticLimit, metavar='N',
                          help='include at most N mismatch paths total (1–1000); omitted by default')
    timed = commands.add_parser('replay-arrivals', help='simulate a bounded first-arrival decision-wait model')
    timed.add_argument('corpus', help='reference OTLP JSON corpus of expected spans')
    timed.add_argument('--config', required=True, help='policies only; Collector runtime settings are unsupported')
    timed.add_argument('--arrivals', required=True, help='version 1 arrival schedule JSON file')
    timed.add_argument('--decision-wait-ms', required=True, type=int, help='positive integer wait in the simulated model')
    verify = commands.add_parser('verify-arrivals', help='check saved single-wait input fingerprints')
    verify.add_argument('corpus', help='reference OTLP JSON corpus of expected spans')
    verify.add_argument('--report', required=True, help='saved schema-1 replay-arrivals JSON report')
    verify.add_argument('--config', required=True, help='supported policy-only configuration')
    verify.add_argument('--arrivals', required=True, help='version 1 arrival schedule JSON file')
    verify.add_argument('--decision-wait-ms', required=True, type=int, help='positive integer wait to verify')
    arrival_evidence = commands.add_parser('verify-arrivals-evidence', allow_abbrev=False, help='compare saved evidence with local arrival replay')
    arrival_evidence.add_argument('corpus', help='reference OTLP JSON corpus of expected spans')
    arrival_evidence.add_argument('--report', required=True, help='saved schema-1 replay-arrivals JSON report')
    arrival_evidence.add_argument('--config', required=True, help='supported policy-only configuration')
    arrival_evidence.add_argument('--arrivals', required=True, help='version 1 arrival schedule JSON file')
    arrival_evidence.add_argument('--decision-wait-ms', required=True, type=int, help='positive integer wait to regenerate')
    arrival_evidence.add_argument('--diagnostic-limit', action=_DiagnosticLimit, metavar='N',
                                  help='include at most N mismatch paths total (1–1000); omitted by default')
    waits = commands.add_parser('compare-arrivals', help='compare two waits in the bounded arrival model')
    waits.add_argument('corpus', help='reference OTLP JSON corpus of expected spans')
    waits.add_argument('--config', required=True, help='shared policies only; no Collector runtime settings')
    waits.add_argument('--arrivals', required=True, help='shared version 1 arrival schedule JSON file')
    waits.add_argument('--baseline-wait-ms', required=True, type=int, help='positive integer baseline wait')
    waits.add_argument('--candidate-wait-ms', required=True, type=int, help='positive integer candidate wait')
    verify_pair = commands.add_parser('verify-compare-arrivals', help='check saved paired-wait input fingerprints')
    verify_pair.add_argument('corpus', help='reference OTLP JSON corpus of expected spans')
    verify_pair.add_argument('--report', required=True, help='saved schema-1 compare-arrivals JSON report')
    verify_pair.add_argument('--config', required=True, help='supported shared policy-only configuration')
    verify_pair.add_argument('--arrivals', required=True, help='shared version 1 arrival schedule JSON file')
    verify_pair.add_argument('--baseline-wait-ms', required=True, type=int, help='positive integer baseline wait to verify')
    verify_pair.add_argument('--candidate-wait-ms', required=True, type=int, help='positive integer candidate wait to verify')
    pair_evidence = commands.add_parser('verify-compare-arrivals-evidence', allow_abbrev=False,
                                        help='compare saved evidence with local paired arrival replay')
    pair_evidence.add_argument('corpus', help='reference OTLP JSON corpus of expected spans')
    pair_evidence.add_argument('--report', required=True, help='saved schema-1 compare-arrivals JSON report')
    pair_evidence.add_argument('--config', required=True, help='supported shared policy-only configuration')
    pair_evidence.add_argument('--arrivals', required=True, help='shared version 1 arrival schedule JSON file')
    pair_evidence.add_argument('--baseline-wait-ms', required=True, type=int, help='positive integer baseline wait to regenerate')
    pair_evidence.add_argument('--candidate-wait-ms', required=True, type=int, help='positive integer candidate wait to regenerate')
    pair_evidence.add_argument('--diagnostic-limit', action=_DiagnosticLimit, metavar='N',
                               help='include at most N mismatch paths total (1–1000); omitted by default')
    pair = commands.add_parser('compare', help='compare finite-corpus retention for two configs')
    pair.add_argument('corpus')
    pair.add_argument('--baseline', required=True)
    pair.add_argument('--candidate', required=True)
    verify_configs = commands.add_parser('verify-compare', help='check saved static comparison input fingerprints')
    verify_configs.add_argument('corpus', help='supported OTLP JSON corpus')
    verify_configs.add_argument('--report', required=True, help='saved schema-1 static comparison JSON report')
    verify_configs.add_argument('--baseline', required=True, help='supported baseline policy-only configuration')
    verify_configs.add_argument('--candidate', required=True, help='supported candidate policy-only configuration')
    compare_evidence = commands.add_parser('verify-compare-evidence', help='compare saved evidence with local configuration comparison')
    compare_evidence.add_argument('corpus', help='supported OTLP JSON corpus')
    compare_evidence.add_argument('--report', required=True, help='saved schema-1 static comparison JSON report')
    compare_evidence.add_argument('--baseline', required=True, help='supported baseline policy-only configuration')
    compare_evidence.add_argument('--candidate', required=True, help='supported candidate policy-only configuration')
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        # Validate all configuration and the entire corpus before emitting anything.
        if args.command == 'replay':
            policies = load_config(args.config)
            result = replay(load_corpus(args.corpus), policies)
        elif args.command == 'verify-replay':
            reported = load_replay_fingerprints(args.report)
            policies = load_config(args.config)
            result = verify_replay(reported, load_corpus(args.corpus), policies)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'verify-replay-evidence':
            reported = load_replay_evidence(args.report)
            policies = load_config(args.config)
            result = verify_replay_evidence(reported, load_corpus(args.corpus), policies,
                                           args.diagnostic_limit)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'replay-arrivals':
            policies = load_config(args.config)
            result = replay_arrivals(load_corpus(args.corpus), policies,
                                     load_arrivals(args.arrivals), args.decision_wait_ms)
        elif args.command == 'verify-arrivals':
            reported = load_arrival_fingerprints(args.report)
            policies = load_config(args.config)
            result = verify_arrivals(reported, load_corpus(args.corpus), policies,
                                     load_arrivals(args.arrivals), args.decision_wait_ms)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'verify-arrivals-evidence':
            reported = load_arrival_evidence(args.report)
            policies = load_config(args.config)
            result = verify_arrival_evidence(reported, load_corpus(args.corpus), policies,
                                            load_arrivals(args.arrivals), args.decision_wait_ms,
                                            args.diagnostic_limit)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'compare-arrivals':
            policies = load_config(args.config)
            result = compare_arrivals(load_corpus(args.corpus), policies,
                                      load_arrivals(args.arrivals), args.baseline_wait_ms,
                                      args.candidate_wait_ms)
        elif args.command == 'verify-compare-arrivals':
            reported = load_arrival_comparison_fingerprints(args.report)
            policies = load_config(args.config)
            result = verify_arrival_comparison(reported, load_corpus(args.corpus), policies,
                                               load_arrivals(args.arrivals), args.baseline_wait_ms,
                                               args.candidate_wait_ms)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'verify-compare-arrivals-evidence':
            reported = load_arrival_comparison_evidence(args.report)
            policies = load_config(args.config)
            result = verify_arrival_comparison_evidence(
                reported, load_corpus(args.corpus), policies, load_arrivals(args.arrivals),
                args.baseline_wait_ms, args.candidate_wait_ms, args.diagnostic_limit)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'verify-compare-evidence':
            reported = load_comparison_evidence(args.report)
            baseline, candidate = load_config(args.baseline), load_config(args.candidate)
            result = verify_comparison_evidence(reported, load_corpus(args.corpus), baseline, candidate)
            exit_code = 0 if result['matches'] else 1
        elif args.command == 'verify-compare':
            reported = load_comparison_fingerprints(args.report)
            baseline, candidate = load_config(args.baseline), load_config(args.candidate)
            result = verify_comparison(reported, load_corpus(args.corpus), baseline, candidate)
            exit_code = 0 if result['matches'] else 1
        else:
            baseline, candidate = load_config(args.baseline), load_config(args.candidate)
            result = compare(load_corpus(args.corpus), baseline, candidate)
        # Diagnostic pointers can contain arbitrary saved member names, including
        # JSON-escaped lone surrogates that cannot be written as UTF-8 literals.
        rendered = json.dumps(result, ensure_ascii='diagnostics' in result,
                              allow_nan=False, indent=2) + '\n'
        sys.stdout.write(rendered)
        return exit_code
    except (InputError, OSError, UnicodeError, RecursionError, ValueError) as exc:
        print(f'otelwrench tailsim: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
