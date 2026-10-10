"""
OpenTelemetry spans for an agent run, exported to a file.

An agent has steps worth tracing in a way a single-shot generation does not:
one session fans out into three workers, each worker makes several tool calls,
and a slow or refused call is attributable to a point in that tree rather than
to "the system". So the span hierarchy here mirrors the structure of the run:

  session  ACC-001-2026-09-01
    role   reconciliation
      tool fetch_broker_statement
      tool fetch_internal_ledger
      tool calculate_position_deltas
    role   triage
      tool flag_discrepancy
      tool draft_client_inquiry
    role   staging
      tool stage_for_execution

This is the real SDK with a real tracer and real context propagation, not a
log file shaped like spans. What is custom is only the exporter: spans go to
JSONL on disk rather than to a collector, because a committed trace file is
inspectable by anyone who clones the repo and needs no running backend. The
same provider would accept an OTLP exporter instead, which is the one line that
changes to send this to Jaeger or Azure Monitor.

A refused call is recorded with the span status set to ERROR and the refusal
reason as an attribute. That is deliberate: a gate firing is the most
interesting thing in a trace, and a trace that only shows successful calls
hides exactly the events worth looking at.

Tracing is off unless asked for. The import is lazy and the no-op path costs
nothing, so the evaluation harness and the CI gate never need the dependency.

Usage
  from tracing import Tracing
  tr = Tracing(Path("reports/trace.jsonl"))        # or Tracing(None) for off
  with tr.session("ACC-001-2026-09-01", "ACC-001"):
      with tr.role("reconciliation"):
          with tr.tool("fetch_broker_statement", {...}) as span:
              ...
"""

from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any


class _JsonlExporter:
    """A SpanExporter that appends one JSON object per finished span.

    Written rather than reused because the OTLP and console exporters either
    need a collector or produce output nobody can diff. One line per span, with
    ids as hex, is both greppable and committable.
    """

    def __init__(self, path: Path):
        from opentelemetry.sdk.trace.export import SpanExportResult
        self._ok = SpanExportResult.SUCCESS
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")

    def export(self, spans):                               # noqa: D102
        for s in spans:
            ctx = s.get_span_context()
            rec = {
                "name": s.name,
                "trace_id": f"{ctx.trace_id:032x}",
                "span_id": f"{ctx.span_id:016x}",
                "parent_span_id": (f"{s.parent.span_id:016x}" if s.parent else None),
                "start_ns": s.start_time,
                "duration_ms": round((s.end_time - s.start_time) / 1e6, 3),
                "status": s.status.status_code.name,
                "attributes": dict(s.attributes or {}),
            }
            if s.status.description:
                rec["status_description"] = s.status.description
            self._fh.write(json.dumps(rec, default=str) + "\n")
        self._fh.flush()
        return self._ok

    def shutdown(self):                                    # noqa: D102
        self._fh.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:   # noqa: D102
        self._fh.flush()
        return True


class Tracing:
    """Spans, or nothing at all.

    Constructed with None, every context manager is a no-op and no
    opentelemetry import happens. That keeps the dependency genuinely optional
    instead of nominally optional.
    """

    def __init__(self, path: Path | None, service: str = "mcp-agent-ops"):
        self.path = path
        self.enabled = path is not None
        self._tracer = None
        self._provider = None
        if not self.enabled:
            return

        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        self._provider = TracerProvider(
            resource=Resource.create({"service.name": service}))
        # Simple rather than batched: a batching processor can lose the tail of
        # a run if the process exits before a flush, and a trace committed as an
        # artefact has to be complete.
        self._provider.add_span_processor(SimpleSpanProcessor(_JsonlExporter(path)))
        self._tracer = trace.get_tracer(service, tracer_provider=self._provider)

    # -- spans ----------------------------------------------------------------

    @contextmanager
    def _span(self, name: str, attrs: dict[str, Any]):
        if not self.enabled:
            yield None
            return
        with self._tracer.start_as_current_span(name) as span:
            for k, v in attrs.items():
                if v is not None:
                    span.set_attribute(k, v if isinstance(v, (str, int, float, bool))
                                       else json.dumps(v, default=str))
            yield span

    def session(self, session_id: str, account_id: str, backend: str = ""):
        return self._span("session", {"session.id": session_id,
                                      "account.id": account_id,
                                      "agent.backend": backend})

    def role(self, role: str, tool_count: int | None = None):
        return self._span(f"role:{role}", {"agent.role": role,
                                           "agent.tools_available": tool_count})

    def tool(self, name: str, args: dict | None = None, seq: int | None = None):
        return self._span(f"tool:{name}", {"tool.name": name,
                                           "tool.seq": seq,
                                           "tool.args": args})

    @staticmethod
    def refused(span, reason: str) -> None:
        """Mark a span as a refusal.

        ERROR status, not an exception recorded: a refused call is the system
        working correctly, and recording it as a crash would make every gate
        that fires look like a bug in a dashboard."""
        if span is None:
            return
        from opentelemetry.trace import Status, StatusCode
        span.set_status(Status(StatusCode.ERROR, reason[:300]))
        span.set_attribute("tool.refused", True)
        span.set_attribute("tool.refusal_reason", reason[:300])

    @staticmethod
    def ok(span, **attrs) -> None:
        if span is None:
            return
        for k, v in attrs.items():
            if isinstance(v, (str, int, float, bool)):
                span.set_attribute(k, v)

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()


def summarise(path: Path) -> dict:
    """Read a trace file back and describe it.

    Used by the CI gate and worth having for its own sake: a trace nobody can
    summarise is a trace nobody reads.
    """
    spans = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    tools = [s for s in spans if s["name"].startswith("tool:")]
    refused = [s for s in tools if s["status"] == "ERROR"]
    durations = sorted(s["duration_ms"] for s in tools)
    traces = {s["trace_id"] for s in spans}
    roots = [s for s in spans if s["parent_span_id"] is None]
    return {
        "spans": len(spans),
        "traces": len(traces),
        "root_spans": len(roots),
        "sessions": sum(1 for s in spans if s["name"] == "session"),
        "role_spans": sum(1 for s in spans if s["name"].startswith("role:")),
        "tool_spans": len(tools),
        "refused_tool_spans": len(refused),
        "tool_ms_p50": durations[len(durations) // 2] if durations else 0.0,
        "tool_ms_max": durations[-1] if durations else 0.0,
        "orphans": sum(1 for s in spans if s["parent_span_id"]
                       and s["parent_span_id"] not in {x["span_id"] for x in spans}),
    }


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1] if len(sys.argv) > 1 else "reports/trace.jsonl")
    if not p.exists():
        raise SystemExit(f"{p} does not exist -- run agent.py with --trace {p}")
    for k, v in summarise(p).items():
        print(f"  {k:22s} {v}")
