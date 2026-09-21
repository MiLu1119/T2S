"""Privacy-conscious OpenTelemetry configuration for VeriSQL Agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased


@dataclass
class TelemetryRuntime:
    provider: TracerProvider | None = None

    def shutdown(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()


def configure_telemetry(settings, exporter_override: SpanExporter | None = None) -> TelemetryRuntime:
    if not settings.tracing_enabled:
        return TelemetryRuntime()

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "1.0.0",
            "deployment.environment.name": settings.otel_environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
    )

    exporter: SpanExporter | None = exporter_override
    if exporter is None and settings.tracing_exporter == "console":
        exporter = ConsoleSpanExporter()
    elif exporter is None and settings.tracing_exporter == "otlp":
        # OTLPSpanExporter reads endpoint, headers and TLS settings from the
        # standard OTEL_EXPORTER_OTLP_* environment variables. This works for
        # Langfuse and other OTLP-compatible backends without vendor secrets in code.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return TelemetryRuntime(provider)


def set_span_attributes(span, attributes: dict[str, Any]) -> None:
    """Set only supported scalar/list attributes and skip absent values."""
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            span.set_attribute(key, value)
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, bool, int, float)) for item in value
        ):
            span.set_attribute(key, value)

