# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared compression core used by both the Claude and Antigravity hooks.

The two platforms integrate differently — Claude rewrites the Bash command to
run through ``wrap.py`` (PreToolUse), while Antigravity compresses captured tool
output (AfterTool) — but the *decision* of what to compress and the
*bookkeeping* afterwards (audit log, savings, mismatch events) are identical.
This module centralizes both so the two entry points stay in lock-step.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from typing import NamedTuple

import src
from src import engine as engine_lib
from src import tracker as tracker_lib

_log = logging.getLogger("token-saver.core")
_log.addHandler(logging.NullHandler())

# Always-on audit log (records processor + ratio, never output content), so
# "why did this route to generic?" is answerable after the fact.  Rotated to
# stay bounded; failures are non-fatal.
_audit = logging.getLogger("token-saver.audit")
_audit.setLevel(logging.INFO)
if not _audit.handlers:
    try:
        _adir = src.data_dir()
        os.makedirs(_adir, exist_ok=True)
        _audit_handler = logging.handlers.RotatingFileHandler(
            os.path.join(_adir, "audit.log"), maxBytes=1_000_000, backupCount=1
        )
        _audit_handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s")
        )
        _audit.addHandler(_audit_handler)
    # This best-effort boundary must not block the host command or hook.
    # pylint: disable-next=broad-exception-caught
    except Exception:
        _audit.addHandler(logging.NullHandler())


class CompressResult(NamedTuple):
    """Outcome of a single compression, independent of engine internals.

    Attributes:
        compressed: Output returned to the host integration.
        processor: Name of the processor supplying the final output.
        was_compressed: Whether output changed, including secret redaction.
        is_mismatch: Whether a specialized processor missed its size target.
        attempted_processor: Name of the originally selected processor.
        original_len: Original output length in characters.
        compressed_len: Returned output length in characters.
    """

    compressed: str
    processor: str
    was_compressed: bool
    is_mismatch: bool
    attempted_processor: str
    original_len: int
    compressed_len: int


def should_compress(command: str) -> bool:
    """Whether ``command`` is eligible for compression (shared gate).

    Delegates to the PreToolUse decision logic so Claude and Antigravity make
    the same call.  Imported lazily to avoid a src->scripts import at module
    load.

    Args:
        command: Shell command text associated with the captured output.

    Returns:
        Whether the shared hook eligibility rules allow compression.
    """
    # Avoid loading the scripts adapter while importing the runtime core.
    # pylint: disable-next=import-outside-toplevel
    from scripts import hook_pretool  # noqa: PLC0415

    return hook_pretool.is_compressible(command)


def compress(
    command: str,
    output: str,
    *,
    engine: engine_lib.CompressionEngine | None = None,
    exit_code: int | None = None,
) -> CompressResult:
    """Compress captured output and fall back when a processor raises.

    An absent engine is constructed before compression. Engine initialization
    errors propagate; failures while compressing return the original output.

    Args:
        command: Shell command text associated with the captured output.
        output: Captured command output before compression.
        engine: Engine to reuse, or None to construct the default engine.
        exit_code: Original command exit status, or None when it is unknown.

    Returns:
        Compressed text and routing/size metadata, with passthrough on a
        processor error.
    """
    engine = engine or engine_lib.CompressionEngine()
    try:
        compressed, processor_name, was_compressed = engine.compress(
            command, output, exit_code=exit_code
        )
    # This best-effort boundary must not block the host command or hook.
    # pylint: disable-next=broad-exception-caught
    except Exception:
        _log.exception("Compression failed for %r — passing through", command)
        compressed, processor_name, was_compressed = (
            output,
            "passthrough",
            False,
        )
    ev = engine.last_event or {}
    return CompressResult(
        compressed=compressed,
        processor=processor_name,
        was_compressed=was_compressed,
        is_mismatch=bool(ev.get("is_mismatch")),
        attempted_processor=ev.get("attempted_processor", processor_name),
        original_len=len(output),
        compressed_len=len(compressed),
    )


def audit_log(
    command: str, processor: str, original_len: int, compressed_len: int
) -> None:
    """Append a single audit line (no output content) — best effort.

    Args:
        command: Shell command text associated with the captured output.
        processor: Stable name of the processor that handled the output.
        original_len: Original output length in characters.
        compressed_len: Compressed output length in characters.
    """
    try:
        ratio = (
            ((original_len - compressed_len) / original_len * 100)
            if original_len > 0
            else 0.0
        )
        _audit.info(
            "processor=%s original=%d compressed=%d ratio=%.1f%% cmd=%r",
            processor,
            original_len,
            compressed_len,
            ratio,
            command[:120],
        )
    # This best-effort boundary must not block the host command or hook.
    # pylint: disable-next=broad-exception-caught
    except Exception:
        _log.debug("Audit logging failed", exc_info=True)


def record_saving(
    command: str,
    processor: str,
    original_len: int,
    compressed_len: int,
    platform: str,
) -> None:
    """Record a savings row — best effort.

    Args:
        command: Shell command text associated with the captured output.
        processor: Stable name of the processor that handled the output.
        original_len: Original output length in characters.
        compressed_len: Compressed output length in characters.
        platform: Name of the host integration recording the event.
    """
    try:
        tracker = tracker_lib.SavingsTracker()
        tracker.record_saving(
            command=command,
            processor=processor,
            original_size=original_len,
            compressed_size=compressed_len,
            platform=platform,
        )
        tracker.close()
    # This best-effort boundary must not block the host command or hook.
    # pylint: disable-next=broad-exception-caught
    except Exception:
        _log.exception("Tracking failed")


def record_mismatches(items: list[tuple[str, str, int]], platform: str) -> None:
    """Record processor-mismatch events in one tracker session — best effort.

    Each item is (command, attempted_processor, original_len).

    Args:
        items: Command, attempted processor, and original character-count
            tuples.
        platform: Name of the host integration recording the event.
    """
    if not items:
        return
    try:
        tracker = tracker_lib.SavingsTracker()
        for command, processor, original_len in items:
            tracker.record_mismatch(
                command=command,
                processor=processor,
                original_size=original_len,
                platform=platform,
            )
        tracker.close()
    # This best-effort boundary must not block the host command or hook.
    # pylint: disable-next=broad-exception-caught
    except Exception:
        _log.exception("Mismatch tracking failed")


def record_result(result: CompressResult, command: str, platform: str) -> None:
    """Audit-log, then record savings and/or mismatch from a CompressResult.

    Args:
        result: Compression outcome whose metadata should be recorded.
        command: Shell command text associated with the captured output.
        platform: Name of the host integration recording the event.
    """
    audit_log(
        command, result.processor, result.original_len, result.compressed_len
    )
    if result.is_mismatch:
        record_mismatches(
            [(command, result.attempted_processor, result.original_len)],
            platform,
        )
    if result.was_compressed:
        record_saving(
            command,
            result.processor,
            result.original_len,
            result.compressed_len,
            platform,
        )
