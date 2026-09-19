---
title: Architecture
description: How Token-Saver separates processors, compression policy, opt-in Delta comparisons, quality checks, platform hooks, and local persistence.
permalink: /architecture/
nav_order: 7
---

# Token-Saver architecture

Token-Saver separates command-specific parsing from compression orchestration,
quality checks, and integrations. These boundaries let contributors add a
processor or evaluate captured output without changing platform hooks.

The design applies SOLID principles at concrete extension points. It is an
incremental separation of responsibilities: built-in processors still read
shared configuration, and the existing CLI and hook integration remain in
place.

## Responsibilities and dependencies

```text
Claude hook -> shell wrapper -----+-> core -> compression engine -> processors
Antigravity hook -----------------+     |             |
                                       v             v
                                  audit / tracker   registry

CLI compress -> stdin -----------+-> evaluation -> Compressor interface
CLI replay -> manifest / files --+                      |
                                              compression engine

Default processor discovery -> processor instances -> registry
Explicit processor instances -----------------------> registry

Claude single-command result -> Delta -> redaction -> processor diagnostics
                                |
                                +-> compare / render <-> snapshot store
CLI delta show / clear ---------------------------------> snapshot store
```

The registry supplies the ordered processor list and named fallback. The engine
uses those objects directly; it does not ask the registry to parse output.

| Module | Responsibility | Boundary |
|---|---|---|
| `src/processors/__init__.py` | Discover built-in and configured user processors. | Imports plugin code and supplies instances; discovery is skipped when callers inject processors. |
| `src/registry.py` | Order active processors, build name lookup, and collect hook patterns. | Accepts instances and disabled names; reads no configuration and imports no plugins. |
| `src/engine.py` | Select processors, apply chaining, cleanup, recovery, and acceptance thresholds. | Accepts optional processors and an `EngineSettings` reader. |
| `src/processors/` | Recognize commands and transform their output. | Implement the `Processor` contract. |
| `src/evaluation.py` | Measure a compression result and check a `QualityPolicy`. | Requires only the small `Compressor` protocol; performs no file or CLI I/O. |
| `src/replay.py` | Validate a replay manifest, read captures, and aggregate evaluations. | Filesystem adapter around evaluation; command labels are never executed. |
| `src/quality_cli.py` | Read CLI arguments/stdin and render quality results. | Connects the default engine to evaluation/replay and maps results to exit codes. |
| `src/core.py` | Share hook result handling, audit logging, and savings/mismatch recording. | Keeps the two platform adapters aligned. |
| `scripts/` and `antigravity/` | Implement host protocols and shell execution where required. | Claude wraps commands before execution; Antigravity receives captured output. |
| `src/tracker.py` | Store and query local savings, sessions, and processor mismatches. | SQLite persistence, separate from output transformation. |
| `src/diagnostics.py` | Define immutable diagnostic and snapshot values. | No persistence; parsers receive sanitized output and report conservative observations. |
| `src/delta.py` | Compare snapshots and render current diagnostic state. | Opt-in orchestration for completed single Claude wrapper commands; does not execute commands. |
| `src/delta_redaction.py` | Mask recognized secret formats before Delta uses captured output. | Deterministic text transformation; not a universal secret detector. |
| `src/delta_store.py` | Retain private, bounded, expiring snapshots. | Separate SQLite database containing already sanitized payloads and hashed scope metadata. |
| `src/delta_cli.py` | Read snapshot details and clear retained history. | CLI presentation and status codes; does not rerun commands. |

## SOLID in practice

| Principle | Concrete boundary |
|---|---|
| Single responsibility | Processors parse; the wrapper executes; the store manages retention; the CLI presents results. |
| Open/closed | A new diagnostic family implements the optional processor method; the engine and wrapper need no tool-specific branch. |
| Substitutability | Existing processors keep their signatures and return shapes; the default diagnostic method declines with `None`. |
| Interface segregation | Ordinary compression does not require diagnostic support, a snapshot store, or a platform host. |
| Dependency inversion | The engine accepts processor instances and settings; quality evaluation accepts the small `Compressor` protocol. Delta's pure rendering can be tested with snapshot values independently of SQLite. |

These are working boundaries, not a requirement to add an interface around every
function. The `open_store()` integration seam supplies the default persistence
adapter; format recognition remains entirely inside processors.

## Processor extension contract

A processor provides a stable name, priority, anchored hook patterns,
`can_handle(command)`, and `process(command, output)`. Discovery finds concrete
processor subclasses automatically. The registry sorts them by `(priority,
name)` and requires exactly one fallback named `generic`, at priority `999`,
after all other processors. Disabling processors does not remove that fallback.

Adding a tool family normally means adding its processor and tests, then updating
its documentation and public counts. It should not require tool-specific
branches in the engine or hooks. See the
[processor reference](processors/index.md) and the
[contributor guide](https://github.com/ppgranger/token-saver/blob/main/CONTRIBUTING.md).

The optional capabilities on `Processor` have behavioral obligations:

- `handles_failure` opts into processing output from a known failed command.
  Otherwise the engine selects the generic fallback for that failure.
- `wants_exit_code` means `process()` also accepts the `exit_code` keyword.
- `redacted_secrets()` identifies calls whose redacted result must survive the
  compression-ratio acceptance check. The engine exposes this fact in
  `last_event["redacted"]`; adapters must not reparse the original output after
  such a result, because custom masking rules may be unknown to them.
- `chain_to` requests secondary processors; the engine bounds chaining and
  avoids revisiting a processor.
- `diagnostics(command, output, *, exit_code=None)` optionally returns a
  `diagnostics.Snapshot` for complete, supported output. Its default returns
  `None`, so existing processors need no changes. The caller sanitizes input
  before parsing; uncertain formats must decline rather than infer results.

Implementations must remain compatible with these contracts. Precision tests,
failure fixtures, and the compression ratchet check concrete cases; they do not
establish that every possible output format is lossless.

## Delta comparison boundary

The experimental [Delta integration](delta.md) operates after a single Claude
wrapper execution. The wrapper first obtains the ordinary compression result,
then offers the completed output and actual status to `delta.apply()`. Disabled
Delta, missing session context, unsupported shell syntax, oversized input, and
unsupported statuses retain ordinary behavior. Chains, dry runs, stdin captures,
Antigravity, and the portable quality commands do not use this integration.
An ordinary result marked as redacted also bypasses Delta: preserving a
processor's masking takes precedence over constructing a snapshot from raw text.

For eligible input, Delta masks recognized secrets before asking
`CompressionEngine.diagnostics()` to delegate to the selected processor. The
engine has no pytest- or Ruff-specific branches. The existing test and lint
processors opt into the diagnostic contract. Parsers provide stable identifiers,
complete diagnostic details, explicitly observed passing test IDs, a current
summary, and retained surrounding context. They perform no persistence.
Pytest traceback frame separators remain inside their failure block. Parametrized
identities are matched against complete failure titles rather than split at
punctuation that may belong to a test parameter.
Repeated multiline short summaries are omitted only after matching a complete
exception block already retained verbatim in the primary traceback. Partial or
different summaries remain in context. Rendering and retrieval therefore retain
the original evidence once without classifying unknown text as boilerplate.

Delta hashes the session, real working directory, exact command, family, schema,
and Token-Saver version into a comparison scope. It reads the latest snapshot,
stores the full current snapshot, and renders the difference. Each current
diagnostic remains named. New and changed details are shown fully; only identical
details are summarized. Absence from the current inventory means `NOT OBSERVED`
unless an explicit passing test observation supports `PASSED`.

The sanitized ordinary result wins when it is no larger than the Delta rendering
and contains every full new or changed diagnostic block. Thus unchanged repeats
emit Delta only when shorter, while fresh diagnostic preservation can produce a
larger result than v2 compression. The stored snapshot becomes a baseline in
either case, so retained baselines do not always have a retrieval hint shown.

`delta_store.Store` owns SQLite transactions, private data paths, a 1 MiB payload
limit, read-time expiry, and a global retained-run cap. It does not sanitize its
own inputs or recover corrupt data by deleting it. The integration owns failure
isolation and records content-free diagnostics. Once masking has occurred,
fallback output must retain it. `delta show` validates a stored snapshot and
sanitizes presentation again; `delta clear` removes history across all scopes.
Insertion order selects the latest snapshot and enforces the count cap. Records
dated in the future after a backward clock adjustment are discarded on access,
so a clock change cannot extend retention or evict a fresh result in their favor.

The three Delta configuration settings are global/environment-only because they
control retaining captured output. They are separate from ordinary compression
thresholds and from the metadata-only savings database. This boundary keeps
stateless `compress()` behavior and existing processor extension points intact.

## Construct an engine with explicit dependencies

An application can choose its processor set without loading configured user
plugins. Supplying settings also separates engine thresholds from the default
configuration reader:

```python
from src import engine
from src.processors import generic
from src.processors import git

compressor = engine.CompressionEngine(
    [git.GitProcessor(), generic.GenericProcessor()],
    settings={
        "enabled": True,
        "disabled_processors": [],
        "min_input_length": 1,
        "min_compression_ratio": 0.0,
        "max_chain_depth": 3,
        "recover_critical_lines": 20,
    },
)

output, processor_name, changed = compressor.compress(
    "git status",
    "On branch main\nnothing to commit, working tree clean\n",
    exit_code=0,
)
```

`EngineSettings` only requires a `get(key)` method. A supplied reader must provide
the engine's required values; a partial mapping is not automatically merged with
defaults. These settings govern the engine, while built-in processor-specific
limits still come from `src.config`. A fully isolated application also needs
processors whose configuration it controls.

With no explicit dependencies, `CompressionEngine()` retains discovery and the
shared configuration reader. Threshold reads remain live across configuration
reloads; the processor registry and its disabled set are assembled at engine
construction. The return shape stays `(output, processor_name, was_compressed)`.
A true flag can represent redaction even when the result is not shorter.

## Evaluate quality independently of storage and the CLI

The `Compressor` protocol requires just the existing `compress()` signature.
The engine satisfies it without inheriting from another base class. Tests or
other applications can supply an alternative implementation of that signature.

```python
from src import evaluation

result = evaluation.evaluate(
    compressor,
    "git status",
    "On branch main\nnothing to commit, working tree clean\n",
    exit_code=0,
    policy=evaluation.QualityPolicy(
        max_tokens=100,
        must_preserve=("main",),
    ),
)

report = result.report()
print(report["passed"], report["compressed_tokens"])
```

A policy can limit estimated output tokens, require a minimum savings percentage,
or require exact substrings to exist in both the original and compressed output.
Violations are reported; evaluation never discards additional text to force a
budget to pass. Token estimates use `ceil(characters / chars_per_token)`, not a
model tokenizer or billing measurement.

`Evaluation` retains compressed text for callers that need it. Its `report()`
contains metrics and violation identifiers, excluding command strings, captured
output, and required substring contents. Replay adds the manifest's case names
and aggregate totals. The `compress --format json` CLI intentionally includes
compressed output as well as those metrics.

## Replay and platform adapters

Replay reads UTF-8 captures named by a versioned JSON manifest. It validates the
schema and limits, bounds input reads, and resolves captures inside the manifest
directory. The adapter supplies captured text and the optional original exit
status to evaluation. It does not run the commands named in the manifest or
record them as new command executions.

`quality_cli.py` owns argument handling and presentation. Quality commands return
status `0` for passing checks, `1` for policy violations, and `2` for invalid
input. New output formats belong in this adapter; quality rules belong in
evaluation; capture-loading changes belong in replay.

The hooks have different responsibilities. Claude's pre-tool adapter checks
eligibility and rewrites accepted commands through `wrap.py`, which executes
them and preserves execution status. Antigravity's after-tool adapter transforms
already captured output. Shared core functions connect compression results to
audit and tracking. The savings SQLite database stores command metadata and size
measurements, not complete captured output; command strings themselves may still
contain sensitive information. Opting into Delta creates a separate store of
sanitized diagnostic snapshots, including captured context, with its own
retention limits. See the [Delta storage details](delta.md#local-storage-and-retention).

These boundaries provide focused places to extend the system while preserving
the existing CLI, processor API, and platform behavior. More configuration
injection and a shared eligibility service remain possible future refactors;
they are not prerequisites for adding a processor or a quality contract.
