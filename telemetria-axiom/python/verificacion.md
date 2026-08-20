# Verificación de la telemetría (sin depender de Axiom)

Dos formas de validar antes de desplegar. Ajustá el import `observability` a la ruta real del módulo.

## 1. Correlación log↔trace (exportadores in-memory de OTel)

Prueba que el `resumen` emitido DENTRO de un `work_span` hereda el `trace_id` del span (y que
fuera del span daría 0). Es la prueba clave de que la correlación funciona.

```python
import logging
from src.helpers import observability as ob
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor

trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
le = InMemoryLogExporter(); lp = LoggerProvider(); lp.add_log_record_processor(SimpleLogRecordProcessor(le))
set_logger_provider(lp)
h = LoggingHandler(level=logging.INFO, logger_provider=lp); h.addFilter(ob._FiltroVolumen())
logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.INFO)

with ob.work_span("mi-operacion"):
    tid = format(trace.get_current_span().get_span_context().trace_id, "032x")
    ob.log_summary("mi-operacion", "core", "success", 1.0, procesados=5)
lp.force_flush()

rec = [l.log_record for l in le.get_finished_logs()
       if (l.log_record.attributes or {}).get("event.type") == "operation_summary"][0]
assert format(rec.trace_id, "032x") == tid   # LOG ATADO AL TRACE
assert rec.attributes["operation.name"] == "mi-operacion" and rec.attributes["procesados"] == 5
print("OK: resumen lleva el trace_id del span")
```

## 2. Camino real de ingest (servidor HTTP local que captura el POST)

Prueba que el exporter pega al endpoint correcto con los headers correctos, sin necesitar un token de
Axiom. El body es protobuf, pero los strings viajan como UTF-8, así que se pueden verificar como
substring.

```python
import os, threading, logging, time
from http.server import BaseHTTPRequestHandler, HTTPServer

posts = []
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        posts.append({"path": self.path, "auth": self.headers.get("Authorization"),
                      "ds": self.headers.get("X-Axiom-Dataset"), "raw": body})
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
os.environ.update({
    "AXIOM_TOKEN": "xaat-test", "AXIOM_DATASET": "mi-logs", "AXIOM_TRACES_DATASET": "mi-traces",
    "AXIOM_ENV": "dev", "OTEL_SERVICE_NAME": "mi-servicio",
    "AXIOM_LOGS_URL": f"{base}/v1/logs", "AXIOM_TRACES_URL": f"{base}/v1/traces",
})
from src.helpers.observability import setup_observability, work_span
setup_observability()
logging.getLogger("app.x").warning("prueba")
with work_span("mi-operacion"):
    pass
from opentelemetry._logs import get_logger_provider
from opentelemetry import trace
get_logger_provider().force_flush(); trace.get_tracer_provider().force_flush(); time.sleep(0.4)

raw = b"".join(p["raw"] for p in posts)
assert any(p["path"] == "/v1/logs" for p in posts)
assert any(p["path"] == "/v1/traces" for p in posts)
assert all(p["auth"] == "Bearer xaat-test" for p in posts)
assert b"mi-servicio" in raw   # resource service.name
print("OK: POST a /v1/logs y /v1/traces con headers correctos")
```

## 3. No-op sin credenciales

```python
import os
for k in ("AXIOM_TOKEN", "AXIOM_DATASET", "AXIOM_TRACES_DATASET"):
    os.environ.pop(k, None)
from src.helpers.observability import setup_observability, work_span, inject_trace_context
setup_observability()                 # no debe hacer nada ni fallar
with work_span("X"):
    pass                              # no-op, ejecuta el cuerpo
msg = {"id": 1}; inject_trace_context(msg)
assert "traceparent" not in msg       # sin span activo, mensaje intacto
print("OK: no-op limpio sin credenciales")
```
