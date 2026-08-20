"""
Observabilidad: logs + traces a Axiom vía OpenTelemetry (OTLP).
==============================================================

Estándar: telemetría por OTLP directo a Axiom, sin Collector. Dos datasets por proyecto:
`{proyecto}-logs` y `{proyecto}-traces`; el campo `service.name` identifica el repo dentro del dataset.
Es agnóstico del tipo de proyecto: API HTTP, worker de colas, job programado, CLI o función
serverless. Lo único que cambia es cuál es la UNIDAD DE TRABAJO (un request, un mensaje, una corrida,
una invocación) y con qué frecuencia ocurre.

  - LOGS: se engancha el `LoggingHandler` de OTel al root logger; los `logging.*(...)` de la app se
    exportan al dataset de logs. Filtro de volumen: a Axiom solo va el resumen estructurado
    (event.type=operation_summary) + todo WARNING/ERROR; el resto de INFO queda en consola.
  - TRACES: `TracerProvider` + OTLPSpanExporter al dataset de traces. Cada unidad de trabajo se
    envuelve en un span con `work_span(name, kind=...)` — kind `server` para un request HTTP,
    `consumer` para un mensaje de cola, `internal` para un job o una corrida de CLI. Propagación
    entre procesos: `inject_trace_context` al encolar/llamar y `consumer_span(carrier=...)` al
    consumir (header W3C `traceparent` dentro del mensaje).
  - Resource attrs: `service.name`, `deployment.environment`, `service.version`, `container.id`. El
    `trace_id` correlaciona ambos datasets: un log emitido DENTRO de un span lleva su trace_id.

VOLUMEN (la regla que define el costo): `log_summary` es para unidades de BAJA frecuencia — un job,
una corrida de CLI, un lote. En unidades de ALTA frecuencia (por-request, por-mensaje) no se emite
resumen: alcanza con el span muestreado (`HIGH_FREQUENCY_SPANS`) y los WARNING/ERROR.

Robustez: TODO gateado por env (sin `AXIOM_TOKEN` -> no-op) y envuelto en try/except — la
observabilidad JAMÁS debe tumbar el arranque ni el procesamiento. Los helpers son no-op si OTel no
está disponible o el provider no se configuró. Nada se hardcodea: endpoints, datasets y service.name
salen de variables de entorno.
"""
import os
import sys
import logging
import inspect
import functools
from contextlib import contextmanager

# --- CONFIG DEL PROYECTO (ajustar por servicio) ------------------------------------------------
# Scope de instrumentación de OTel (NO es el service.name; es un identificador de código, típicamente
# la ruta del módulo).
_INSTRUMENTATION_SCOPE = "src.helpers.observability"
# Spans de ALTA FRECUENCIA a muestrear (en vez de exportar el 100%). Poné acá los nombres de los spans
# que se generan muchísimo: rutas HTTP con tráfico, consumidores de cola por mensaje, handlers
# serverless calientes. Vacío = exportar todos los spans.
HIGH_FREQUENCY_SPANS: set = set()

# --- Política de volumen de LOGS ---------------------------------------------------------------
SUMMARY_EVENT = "operation_summary"
# Output de estos loggers NO se exporta (evita loopear con el HTTP del exporter y ruido de SDKs).
# Agregá acá cualquier logger de infraestructura ruidoso de tu proyecto.
LOGGERS_EXCLUIDOS = ("urllib3", "requests", "opentelemetry", __name__)

# OTel es opcional en tiempo de import (aunque va en requirements): si no está, los helpers son no-op.
try:
    from opentelemetry import trace as _trace
    from opentelemetry.trace import SpanKind as _SpanKind
    from opentelemetry.propagate import inject as _inject, extract as _extract
    _KINDS = {
        "internal": _SpanKind.INTERNAL, "server": _SpanKind.SERVER,
        "consumer": _SpanKind.CONSUMER, "producer": _SpanKind.PRODUCER, "client": _SpanKind.CLIENT,
    }
except Exception:  # OTel no disponible: modo no-op. Se dejan los nombres usados por los helpers
    _KINDS = {}     # SIEMPRE asignados (los helpers chequean `is not None`); evita "posiblemente
    _trace = None   # sin asignar" en el analizador estático.
    _inject = None
    _extract = None

_traces_ready = False


def _high_frequency_sample_rate() -> float:
    try:
        rate = float(os.getenv("OTEL_HIGH_FREQUENCY_SAMPLE_RATE", "0.1"))
    except ValueError:
        rate = 0.1
    return min(1.0, max(0.0, rate))


class _FiltroVolumen(logging.Filter):
    """WARNING+ siempre; INFO solo para resúmenes estructurados; nunca loggers de infraestructura."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (record.name or "").startswith(LOGGERS_EXCLUIDOS):
            return False
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno >= logging.INFO:
            return record.__dict__.get("event.type") == SUMMARY_EVENT
        return False


def _resource_attributes(env: str) -> dict:
    attrs = {"deployment.environment": env}
    service_name = os.getenv("OTEL_SERVICE_NAME")
    if service_name:
        attrs["service.name"] = service_name
    else:
        print("[obs] OTEL_SERVICE_NAME no seteada -> resource sin service.name", file=sys.stderr)
    version = os.getenv("OTEL_SERVICE_VERSION")
    if version:
        attrs["service.version"] = version
    container_id = os.getenv("HOSTNAME")  # en Docker, HOSTNAME es el id del contenedor
    if container_id:
        attrs["container.id"] = container_id
    return attrs


def _setup_logs(token: str, dataset: str, env: str) -> bool:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, LoggingHandler):
            return False  # ya registrado

    url = os.getenv("AXIOM_LOGS_URL")
    if not url:
        print("[obs] AXIOM_LOGS_URL no seteada -> logs a Axiom deshabilitados", file=sys.stderr)
        return False
    exporter = OTLPLogExporter(
        endpoint=url, headers={"Authorization": f"Bearer {token}", "X-Axiom-Dataset": dataset}
    )
    provider = LoggerProvider(resource=Resource.create(_resource_attributes(env)))
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)  # registra su propio flush en atexit

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    handler.addFilter(_FiltroVolumen())
    if root.getEffectiveLevel() > logging.INFO:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    return True


def _setup_traces(token: str, dataset: str, env: str) -> bool:
    global _traces_ready
    from opentelemetry import trace
    from opentelemetry.trace import ProxyTracerProvider
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, Sampler, TraceIdRatioBased
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if not isinstance(trace.get_tracer_provider(), ProxyTracerProvider):
        return False  # ya hay un TracerProvider real configurado

    url = os.getenv("AXIOM_TRACES_URL")
    if not url:
        print("[obs] AXIOM_TRACES_URL no seteada -> traces a Axiom deshabilitados", file=sys.stderr)
        return False
    exporter = OTLPSpanExporter(
        endpoint=url, headers={"Authorization": f"Bearer {token}", "X-Axiom-Dataset": dataset}
    )
    high_frequency_rate = _high_frequency_sample_rate()

    class _SelectiveRootSampler(Sampler):
        """Muestrea los spans de alta frecuencia; el resto se exporta siempre. ParentBased hace que
        los hijos sigan la decisión del root, así un trace no queda a medias."""
        def __init__(self, rate: float):
            self._ratio = TraceIdRatioBased(rate)

        def should_sample(self, parent_context, trace_id, name, kind=None,
                          attributes=None, links=None, trace_state=None):
            sampler = self._ratio if name in HIGH_FREQUENCY_SPANS else ALWAYS_ON
            return sampler.should_sample(
                parent_context, trace_id, name, kind, attributes, links, trace_state
            )

        def get_description(self):
            return f"SelectiveRootSampler{{high_frequency_rate={high_frequency_rate}}}"

    sampler = ParentBased(root=_SelectiveRootSampler(high_frequency_rate))
    provider = TracerProvider(resource=Resource.create(_resource_attributes(env)), sampler=sampler)
    provider.add_span_processor(BatchSpanProcessor(exporter))  # flush en atexit
    trace.set_tracer_provider(provider)
    _traces_ready = True
    return True


def setup_observability():
    """Configura logs y traces a Axiom si hay credenciales. No propaga errores (no debe tumbar el app)."""
    token = os.getenv("AXIOM_TOKEN")
    if not token:
        logging.getLogger(__name__).debug("[obs] AXIOM_TOKEN no seteado -> observabilidad deshabilitada")
        return
    env = os.getenv("AXIOM_ENV") or "unknown"

    dataset_logs = os.getenv("AXIOM_DATASET")
    if dataset_logs:
        try:
            if _setup_logs(token, dataset_logs, env):
                logging.getLogger(__name__).info("[obs] logs -> Axiom '%s' (env=%s)", dataset_logs, env)
        except Exception as e:
            print(f"[obs] setup de logs falló: {e}", file=sys.stderr)

    dataset_traces = os.getenv("AXIOM_TRACES_DATASET")
    if dataset_traces:
        try:
            if _setup_traces(token, dataset_traces, env):
                logging.getLogger(__name__).info("[obs] traces -> Axiom '%s' (env=%s)", dataset_traces, env)
        except Exception as e:
            print(f"[obs] setup de traces falló: {e}", file=sys.stderr)


def log_summary(operation: str, component: str, outcome: str, duration_ms: float, **attributes):
    """Emite el ÚNICO INFO operativo que se exporta a Axiom por unidad de trabajo. Llamalo UNA vez por
    ejecución del entry point, DENTRO del `with work_span(...)` para que herede el trace_id.

    SOLO para unidades de BAJA FRECUENCIA (job programado, corrida de CLI, lote completo). En una
    unidad de alta frecuencia (por-request, por-mensaje) NO llames a esto: el span ya mide y este log
    dispararía el costo; resumí a nivel del lote o dejá que hablen los spans + los errores.

    operation: nombre estable de la unidad ('sync-inventario', 'GET /productos', 'procesar-lote').
    component: subsistema al que pertenece ('inventario', 'api', 'etl').
    outcome:   'success' | 'partial' | 'empty' | 'error' | 'timeout' | ...
    attributes: pares clave=valor escalares (métricas y contexto: items=120, queue__name='skus').
                Usá '__' en la clave para que en Axiom quede con punto: queue__name -> 'queue.name'.
    """
    payload = {
        "event.type": SUMMARY_EVENT,
        "operation.name": operation,
        "component": component,
        "outcome": outcome,
        "duration_ms": round(float(duration_ms), 2),
    }
    for key, value in attributes.items():
        if value is None:
            continue
        name = key.replace("__", ".")
        payload[name] = value if isinstance(value, (str, bool, int, float)) else str(value)

    logging.getLogger("app.summary").info(
        "[SUMMARY] %s outcome=%s duration_ms=%.2f", operation, outcome, float(duration_ms), extra=payload
    )


# --- Helpers de instrumentación (no-op si traces no está configurado) ---------------------------

def _tracer():
    return _trace.get_tracer(_INSTRUMENTATION_SCOPE) if _trace is not None else None


@contextmanager
def work_span(name: str, kind: str = "internal", attributes: dict = None):
    """Abre el span de una unidad de trabajo como current. `kind`: 'server' (request HTTP entrante),
    'consumer' (mensaje de cola), 'producer' (encolar), 'client' (llamada saliente), 'internal' (job,
    CLI, cómputo). start_as_current_span registra la excepción, marca status ERROR y la RE-LANZA (no
    la traga). ENVOLVÉ con esto el try/except del entry point (span afuera, try adentro) para que
    log_summary y los logging.error hereden el trace_id."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(
        name, kind=_KINDS.get(kind, _KINDS["internal"]), attributes=attributes
    ) as span:
        yield span


@contextmanager
def consumer_span(name: str, carrier: dict = None, attributes: dict = None):
    """Span CONSUMER que continúa el trace propagado en un mensaje de cola (carrier con 'traceparent')."""
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    ctx = None
    if carrier and _extract is not None:
        try:
            ctx = _extract(carrier)
        except Exception:
            ctx = None
    with tracer.start_as_current_span(
        name, context=ctx, kind=_KINDS["consumer"], attributes=attributes
    ) as span:
        yield span


def inject_trace_context(carrier: dict) -> dict:
    """Inyecta el 'traceparent' del span activo en el dict del mensaje (para propagar por la cola).
    Llamalo justo antes de encolar, DENTRO del span del productor."""
    if _inject is not None:
        try:
            _inject(carrier)
        except Exception:
            pass
    return carrier


def traced(name: str, kind: str = "internal"):
    """Decorador (sync o async) que envuelve una función en un `work_span`. OJO: en Azure Functions v2
    NO decores los handlers registrados (la introspección del SDK puede romperse); ahí preferí envolver
    la llamada interna con `with work_span(...)`. Útil para funciones normales."""
    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrap(*a, **k):
                with work_span(name, kind):
                    return await fn(*a, **k)
            return awrap

        @functools.wraps(fn)
        def wrap(*a, **k):
            with work_span(name, kind):
                return fn(*a, **k)
        return wrap
    return deco
