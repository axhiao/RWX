# RWX Blast Radius

A test selector for the [Builders Blast Radius track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). It reads each patch against SQLGlot revision `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and prints the worker assignment as one JSON line.

## Results

The latest reported hidden practice score before this change was **222 s**, with a target below **130.7 s**. The local comparison starts at repository revision `723c312`. The new hidden score requires a submission.

| Local batch | Patches | Previous simulated seconds | New simulated seconds |
| --- | ---: | ---: | ---: |
| Official public sample | 30 | 94.373 | 94.373 |
| Earlier dialect and optimizer mutations | 22 | 92.400 | 92.400 |
| Shared fixtures and parser branches | 6 | 140.689 | 44.488 |
| Random generator dispatch entries | 3 | 110.451 | 6.522 |
| Random comparisons inside short-circuit expressions | 4 | 15.846 | 11.539 |
| All patches in one batch | 65 | 453.759 | 249.322 |

Each additional patch was labeled by running the full pinned SQLGlot pytest suite separately. Multiline subtest names and errors loading whole test modules are included in the label audit. Both selectors covered every failing test in the combined batch. The new cases deliberately exercise specific parts of SQLGlot. Their frequencies on the hidden batch are unknown. The official scorer also charges all selector and child-process CPU time. The median local total over three native macOS runs was 473.205 s for the previous version and 272.264 s for this version.

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
7. A separate baseline trace records entry into individual expressions, including operands guarded by short-circuit evaluation. Each test module is also imported in a fresh interpreter. The trace only narrows substitutions it can align structurally. Missing or opaque observations retain the existing bound. The value profile follows dictionary copies into generator dispatch caches and records scalar ordering and identity at comparisons.
8. For shared TPC fixtures, the verifier compares the original and changed rows using the upstream loader and follows the lists actually read by each test. Malformed fixture metadata retains the candidates that depend on class setup. For the parser node-limit test, baseline traces distinguish its six calls. The verifier checks each input and option, invokes the affected calls, and verifies the original unit test's assertions.
9. The scheduler tries one through eight workers, assigning the longest tests first. It minimizes the largest assigned load plus 2,000 milliseconds per worker among the candidate schedules. Test IDs are mapped to the numbers in the current input catalog.

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
