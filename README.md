# RWX Blast Radius

A test selector for the [Builders Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). It reads each patch against SQLGlot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and prints the worker assignment as one JSON line.

## Results

The submitted revision `684aa7b` scored **228.6 s** on the hidden practice batch. The table compares simulated seconds on patches for which the expected failures are available locally.

| Batch | Patches | `684aa7b` | Current selector |
| --- | ---: | ---: | ---: |
| Official public sample | 30 | 97.144 | 94.373 |
| Dialect settings, dispatch and comparisons, first seed | 8 | 66.553 | 37.015 |
| Same mutation generator, independent second seed | 8 | 23.061 | 13.662 |
| Optimizer copy flags, branches and comparisons | 6 | 66.285 | 41.723 |

Both revisions covered every failing test on these 52 patches. With all 52 patches delivered in one batch on native macOS, the median total over three runs, including CPU, fell from 255.843 s to 206.406 s. The current batch used 186.773 simulated seconds. The Linux/amd64 Docker public sample reported 94.373 simulated seconds with zero misses. Each additional patch was labeled by a separate run of the full pinned SQLGlot pytest suite. The official scorer charges the selector's CPU time in addition to simulated seconds. The live verifier uses a time budget, so machine speed and batch composition can affect both. A new hidden practice score requires a competition run.

## Run

From the repository root, with Docker running and Python 3 installed on the host:

```sh
make sample
```

This builds for Linux/amd64, runs with the competition's read-only filesystem and resource limits, and scores the predictions. The Dockerfile installs the pinned dependencies used to verify dialect, optimizer and executor tests. The first sample run downloads the public starter kit into `.cache/sample-kit`. Set `KIT=/absolute/path/to/unpacked-kit` to reuse a download. The container receives only the patch directory and test catalog.

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
5. For string-valued dialect tables, the index traces keyed lookups on the effective maps and sets after metaclass initialization. At runtime, a fresh process initializes the patched dialects. The selector compares their forward tables, inverse tables and tries, and selects the tests that observed a changed value, membership, length or iteration result. Expression argument declarations also carry their origins into validation of missing required arguments.
6. The verifier executes selected tests in an isolated overlay of the patched source. It can limit a large fixture-based method to the SQL pairs that reached a changed function body. For TPC-DS it can also compare the outputs of affected parser, optimizer, transpiler and Python-generator calls on recorded pristine inputs. If a call differs, the verifier checks the original assertions for the corresponding queries when they fit the budget. Failed checks, skipped tests, unavailable dependencies and timeouts retain candidates. All worker tasks executed inline still serialize their arguments and results. Running without `--replay` selects tests using static analysis and baseline coverage only.
7. The scheduler tries one through eight workers, assigning the longest tests first. It minimizes the largest assigned load plus 2,000 milliseconds per worker among the candidate schedules. Test IDs are mapped to the numbers in the current input catalog.

Coverage describes behavior on the base revision. New execution paths, environment differences and previously unseen inputs can change which tests fail.

## Regenerate the index

Normal builds use the checked-in files in `maps/`. To regenerate them, use CPython 3.13, a clean checkout at the pinned revision and the dependencies from the official kit. Activate the virtual environment so `python` also resolves inside the SQLGlot subprocess tests.

```sh
make kit
python3.13 -m venv .cache/index-env
. .cache/index-env/bin/activate
python -m pip install -r .cache/sample-kit/requirements.lock
make index PYTHON=python SOURCE=/tmp/rwx-sqlglot-base
```

The index builders read the pristine checkout and test catalog. The fixture map contains 4,035 optimizer fixture entries. All 3,027 distinct executor call captures reproduced their baseline outputs during index generation. The scorer alone reads the public sample answers. The supplied map completed all 1,620 numbered baseline tests on macOS. Its keys are source paths, line numbers, symbols and test IDs, so it can also be regenerated on Linux.

## Competition entry

The entry consists of the public repository URL and a full Git commit hash. The Dockerfile is at the repository root. To identify a revision after committing the project, run `git rev-parse HEAD`.
