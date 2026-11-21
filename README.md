# RWX Blast Radius

A test selector for the [Builders Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). It reads each patch against SQLGlot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and prints the worker assignment as one JSON line.

## Results

| Local evaluation | Result |
| --- | --- |
| Official sample, 30 patches, Linux/amd64 Docker with competition resource limits | 120.119 simulated seconds, 0 missed commits |
| Native macOS, CPython 3.13, three runs | About 0.4 CPU seconds per batch |
| Additional two-file mutation, full SQLGlot pytest suite | 6 selected tests covered all 4 failures, including a failed subtest |

The extra mutation altered Fabric data type precision handling and a SQL fixture used by the optimizer. Practice and final use hidden batches. Their scores include CPU time measured on the competition server.

## Run

From the repository root, with Docker running and Python 3 installed on the host:

```sh
make sample
```

This builds for Linux/amd64, runs with the competition's read-only filesystem and resource limits, and scores the predictions. The first run downloads the public starter kit into `.cache/sample-kit`. Set `KIT=/absolute/path/to/unpacked-kit` to reuse a download. The container receives only the patch directory and test catalog.

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
4. The scheduler tries one through eight workers, assigning the longest tests first. It minimizes the largest assigned load plus 2,000 milliseconds per worker among the candidate schedules. Test IDs are mapped to the numbers in the current input catalog.

Coverage describes behavior on the base revision. New execution paths, environment differences and previously unseen inputs can change which tests fail.

## Regenerate the index

Normal builds use the included `maps/coverage.json`. To rebuild it, use CPython 3.13, a clean checkout at the pinned revision and the dependencies from the official kit. Activate the virtual environment so `python` also resolves inside the SQLGlot subprocess tests.

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
