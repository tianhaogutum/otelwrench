# Reproducible finite-capture experiment

A seeded, synthetic 20-point capture shows the tradeoff between a twitchy and a
damped alert rule. No external data, API, model, tuning search or benchmark
server is used. Reproduce from a clone of the repository:

```sh
otelwrench alert-flapsim compare examples/alert-flapsim/jitter-query-range.json \
  --baseline examples/alert-flapsim/baseline.json \
  --candidate examples/alert-flapsim/candidate.json
```

The generator uses seed 79 in a fixed 32-bit linear congruential generator
(`state = 1664525 * state + 1013904223 mod 2^32`). It applies noise in [-0.01,0.01)
around 0.98 or 1.02, rounded to six decimal places (ECMAScript `toFixed(6)`
rounding), so classification against `>1` is stable. The chosen 20-point pattern
at 60-second intervals is:

```text
Time:   0  60 120 180 240 300 360 420 480 540 600 660 720 780 840 900 960 1020 1080 1140
Truth:  F   T   T   F   T   T   F   T   T   F   F   F   T   T   F   T   T    F    F    F
```

The generator and the experiment summary live in the test suite
(`tests/flapsim/support/__init__.py`: `generate_jitter`, `experiment_result`).
`tests/flapsim/test_cli.py` regenerates both and asserts they are byte-identical
to `examples/alert-flapsim/jitter-query-range.json` and
`examples/alert-flapsim/experiment-result.json`.

The baseline uses for=0s, hold=0s. The candidate uses for=60s, hold=120s.
Both use threshold `>1`, missingData=error, and the same [0,1140] capture.
`examples/alert-flapsim/experiment-result.json` is the saved summary.

| Observed metric | Baseline | Candidate | Candidate minus baseline |
| --- | ---: | ---: | ---: |
| First firing timestamp (seconds) | 60 | 120 | +60 |
| Firing episodes | 5 | 2 | -3 |
| Resolutions | 5 | 2 | -3 |
| Observed firing duration (seconds) | 600 | 900 | +300 |
| Episodes still firing at capture end | 0 | 0 | 0 |

These results are checked independently of the replay algorithm: baseline
firing intervals are [60,180), [240,360), [420,540), [720,840), [900,1020).
Five intervals of 120 seconds total 600 seconds. Candidate intervals are
[120,660) and [780,1140), totaling 540+360=900 seconds. False at 540 starts the
first eventual resolution clock; 540+120=660. False at 1020 starts the second;
1020+120=1140. Shorter false runs earlier in each episode are interrupted by
true, which clears their hold timers.

This is a finite chosen scenario demonstrating a tradeoff: fewer resolutions
come with delayed first firing and additional time firing during false
conditions. It demonstrates no production incident reduction, general
statistical advantage or runtime performance gain. Changing the capture boundary
can change counts and censor episodes. No threshold or configuration is selected
as best; both configurations are explicit inputs. For full alert-rule unit tests,
see [promtool](https://github.com/prometheus/prometheus/blob/main/docs/command-line/promtool.md),
which is a different workflow and is not executed or required here.
