# RWX Blast Radius

A test selector for the [Builders Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). It reads each patch against SQLGlot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and prints the worker assignment as one JSON line.

## Results

| Evaluation | Submitted revision 218011d | Current revision |
| --- | ---: | ---: |
| Public sample, 30 patches, simulated seconds | 120.119 | 97.144 |
| Public sample, native macOS, median total including CPU over three runs | 120.5 | 99.14 |
| Eight additional mutations, simulated seconds | 71.717 | 66.553 |
| Missed commits across these 38 local patches | 0 | 0 |

The reported hidden practice score for 218011d was 232 s. The new hidden score requires a submission. For the additional evaluation, a seeded generator changed dialect settings, dictionary entries, parser dispatch functions, and numeric comparisons in the pinned source. Each patch was run separately through the full SQLGlot pytest suite, including subtest outcomes. Five of the eight patches caused failures. The new native median including CPU was 67.08 s on this batch.

The live verifier has a time budget and retains the static selection if a candidate group times out, so CPU speed can affect the simulated score as well as the measured CPU cost.

## Run

From the repository root, with Docker running and Python 3 installed on the host:

```sh
make sample
```

This builds for Linux/amd64, runs with the competition's read-only filesystem and resource limits, and scores the predictions. The Dockerfile installs the locked dialect-test dependencies from PyPI. The first sample run downloads the public starter kit into `.cache/sample-kit`. Set `KIT=/absolute/path/to/unpacked-kit` to reuse a download. The container receives only the patch directory and test catalog.

For native execution with a local copy of the pinned source:

```sh
git clone https://github.com/tobymao/sqlglot.git /tmp/rwx-sqlglot-base
git -C /tmp/rwx-sqlglot-base checkout --detach eb11aec4be8cb2122afe713a4816662c3f0fb2fa
make sample-native PYTHON=python3.13 SOURCE=/tmp/rwx-sqlglot-base
python3.13 -m unittest discover -s tests -v
```

## How the selection works

1. The offline index records test-by-test Python line coverage, paths opened, expression classes instantiated, dialects used, and operand types observed at identity comparisons. Each test module is also imported in a fresh interpreter to capture initialization paths. Setup and cleanup contribute to the whole class. Process-pool tasks run inline with serialized arguments and results so their computation and serialization appear in the trace.
2. The selector applies each Python hunk to the pristine source in memory and checks its syntax and AST. It unions dependencies across all edited files. Fixture changes use the recorded readers. For multiline branches it includes the entry to the condition, accounting for short-circuit evaluation.
3. For declarative changes it can use the owning dialect, expression class, and covered reads of configuration fields. For an identity-to-equality edit it checks whether the observed operand types make that change equivalent. Unsupported patch formats, compilation failures, and potentially disruptive module initialization changes trigger a wider selection.
4. The value profile records reads of individual dictionary entries, concrete configuration fields and scalar comparison operands. It preserves value origins across dictionary spreads. Unresolved import-time reads and unobserved operand types retain conservative coverage. Structural AST checks verify that the refined changes account for the entire edit.
5. For trusted production-code patches with small, inexpensive groups of dialect tests, the selector executes the whole group in a separate process against an overlay of the patched source. A complete pass, including all subtests, clears the group. Failures, skipped tests and timeouts keep the static candidates. This uses CPU time billed by the competition. For static analysis alone, invoke `python selector.py` without `--replay`.
6. The scheduler tries one through eight workers, assigning the longest tests first. It minimizes the largest assigned load plus 2,000 milliseconds per worker among the candidate schedules. Test IDs are mapped to the numbers in the current input catalog.

Coverage describes behavior on the base revision. New execution paths, environment differences and previously unseen inputs can change which tests fail.

## Regenerate the index

Normal builds use the included `maps/coverage.json` and `maps/profiles.json`. To rebuild it, use CPython 3.13, a clean checkout at the pinned revision and the dependencies from the official kit. Activate the virtual environment so `python` also resolves inside the SQLGlot subprocess tests.

```sh
make kit
python3.13 -m venv .cache/index-env
. .cache/index-env/bin/activate
python -m pip install -r .cache/sample-kit/requirements.lock
make index PYTHON=python SOURCE=/tmp/rwx-sqlglot-base
```

The indexer reads the pristine checkout and test IDs. The scorer alone reads the public sample answers. The supplied map completed all 1,620 numbered baseline tests on macOS. Its keys are source paths, line numbers, symbols and test IDs, so it can also be regenerated on Linux.

## Competition entry

The entry consists of the public repository URL and a full Git commit hash. The Dockerfile is at the repository root. To identify a revision after committing the project, run `git rev-parse HEAD`.
