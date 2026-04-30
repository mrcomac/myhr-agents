"""
observability.py — OpenTelemetry bootstrap for the agents Python services.

Call init_otel(service_name) once at process/worker start. It wires OTLP/gRPC
exporters for traces and metrics pointing at OTEL_EXPORTER_OTLP_ENDPOINT
(default: SigNoz gateway collector). When the env var is empty the SDK is
left uninitialized and all instrumentation calls become no-ops.

Auto-instrumentation is added explicitly (rather than via opentelemetry-instrument)
because uvicorn workers fork after import and pre-fork SDK state breaks gRPC
channels. Initializing inside the worker process avoids that.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_initialized = False


def init_otel(service_name: str) -> None:
    """Initialize tracing + metrics + library auto-instrumentation. Idempotent."""
    global _initialized
    if _initialized:
        return

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — skipping OTel init")
        return

    # OTEL_SERVICE_NAME wins over the argument when set; matches SDK behaviour.
    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)

    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create()  # picks up OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(insecure=True)))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter(insecure=True))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    _instrument_libraries()
    _initialized = True
    log.info("OpenTelemetry initialized service=%s endpoint=%s", service_name, endpoint)


def instrument_fastapi(app) -> None:
    """Attach FastAPI instrumentation. Safe to call when init_otel was a no-op."""
    if not _initialized:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)


def _instrument_libraries() -> None:
    """Wire auto-instrumentation for everything the agents use."""
    # Each instrumentor import is wrapped so a missing extra doesn't crash boot.
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("requests instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
        URLLib3Instrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("urllib3 instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("httpx instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.pika import PikaInstrumentor
        PikaInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("pika instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("redis instrumentation skipped: %s", exc)

    # OpenInference Bedrock instrumentor — emits gen_ai.* semantic attributes
    # (model id, prompts, completions, token usage). Per SigNoz's Bedrock guide.
    # Replaces the generic botocore instrumentor for Bedrock calls; we don't
    # call any other AWS service from boto3 in the agents.
    try:
        from openinference.instrumentation.bedrock import BedrockInstrumentor
        BedrockInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("bedrock instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
        SystemMetricsInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("system_metrics instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        LoggingInstrumentor().instrument(set_logging_format=True)
    except Exception as exc:  # pragma: no cover
        log.warning("logging instrumentation skipped: %s", exc)
