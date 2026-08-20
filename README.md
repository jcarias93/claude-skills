# claude-skills

Skills para [Claude Code](https://claude.com/claude-code). Cada carpeta es una skill autocontenida:
un `SKILL.md` con el estándar y las lecciones aprendidas, más los templates que Claude copia y adapta
al proyecto en el que estés trabajando.

| Skill | Qué hace | Lenguajes |
|---|---|---|
| [`telemetria-axiom/`](telemetria-axiom/) | Telemetría OpenTelemetry → Axiom: logs estructurados + traces correlacionados por `trace_id`, vía OTLP directo (sin Collector) | Python · Node.js/TS · C# · Rust |

---

## Instalar

Skill **personal** (disponible en todos tus proyectos). El symlink es lo recomendado: editás en un
solo lugar y podés commitear los cambios al repo.

```bash
git clone https://github.com/jcarias93/claude-skills.git
ln -s "$PWD/claude-skills/telemetria-axiom" ~/.claude/skills/telemetria-axiom
```

Skill **de proyecto** (versionada junto al código, compartida con el equipo):

```bash
cp -R claude-skills/telemetria-axiom /ruta/al/proyecto/.claude/skills/
```

Verificá que Claude Code la vea con `/skills` — debería aparecer `telemetria-axiom` en la lista.

---

# Skill: `telemetria-axiom`

## Qué resuelve

Un servicio o worker (timers, colas, HTTP) en el que no podés ver si sus operaciones corren, cuánto
tardan y si fallan. La skill instrumenta el servicio para que exporte a Axiom:

- **Logs** — un resumen estructurado por corrida (`event.type=run_summary`) + todo WARNING/ERROR.
- **Traces** — un span por punto de entrada, con propagación de contexto a través de las colas.
- **Correlación** — el `trace_id` cruza ambos datasets: de un log de error saltás al trace completo.

Todo por **OTLP directo a Axiom, sin Collector**, y **100% configurado por variables de entorno**:
sin `AXIOM_TOKEN` la instrumentación es no-op y la app corre igual.

## Antes de empezar (los provee el dueño de la org de Axiom)

1. Dos datasets: `{proyecto}-logs` y `{proyecto}-traces`.
2. Un **token de ingest** (`xaat-…`, solo escritura, scopeado a esos dos datasets). No un token
   personal ni de admin.
3. Un usuario/membresía en la org de Axiom para poder *ver* los datos (el token solo escribe).

## Cómo se usa

La skill se activa sola cuando le pedís observabilidad a Claude Code. Basta con algo así:

```
Agregá telemetría a este servicio: quiero ver en Axiom si los timers corren y poder
cruzar los errores con su trace.
```

O invocándola explícitamente:

```
Usá la skill telemetria-axiom para instrumentar este worker.
```

Claude entonces:

1. Detecta el lenguaje y copia el template correspondiente al proyecto (ej. a
   `src/helpers/observability.py`), sin reescribir la lógica desde cero.
2. Ajusta la config del template: el scope de instrumentación y qué spans son de alta frecuencia.
3. Agrega las dependencias de OTel al manifiesto (`requirements.txt`, `package.json`, `.csproj`, `Cargo.toml`).
4. Llama a `setup_observability()` una vez al arranque.
5. Envuelve cada punto de entrada en un span, con el `run_summary` y los errores **adentro** del span.
6. En las colas, inyecta el `traceparent` al encolar y lo extrae al consumir.
7. Te deja las consultas APL listas para pegar en Axiom.

## Variables de entorno

Nada se hardcodea. Sin `AXIOM_TOKEN` (o sin las URLs) → no-op.

| Variable | Ejemplo | Qué es |
|---|---|---|
| `AXIOM_TOKEN` | `xaat-…` | Token de ingest |
| `AXIOM_DATASET` | `{proyecto}-logs` | Dataset de logs |
| `AXIOM_TRACES_DATASET` | `{proyecto}-traces` | Dataset de traces |
| `AXIOM_ENV` | `prod` / `dev` | `deployment.environment` |
| `AXIOM_LOGS_URL` | `https://api.axiom.co/v1/logs` | Endpoint OTLP de logs |
| `AXIOM_TRACES_URL` | `https://api.axiom.co/v1/traces` | Endpoint OTLP de traces |
| `OTEL_SERVICE_NAME` | `mi-servicio` | `service.name` — identifica el repo dentro del dataset |
| `OTEL_SERVICE_VERSION` | build id | Opcional |
| `OTEL_HIGH_FREQUENCY_SAMPLE_RATE` | `0.1` | Opcional — muestreo de spans de alta frecuencia |

## Cómo queda el código (ejemplo en Python)

**Arranque** — una sola vez:

```python
from src.helpers.observability import setup_observability

setup_observability()   # no-op si no hay AXIOM_TOKEN; nunca tumba el arranque
```

**Un punto de entrada** — span afuera, try/except adentro, para que el resumen y los errores
hereden el `trace_id`:

```python
from src.helpers.observability import job_span, log_run_summary

def procesar_lote():
    t0 = time.perf_counter()
    with job_span("procesar_lote", kind="internal"):
        procesados, outcome = 0, "success"
        try:
            procesados = hacer_trabajo()
        except Exception:
            outcome = "error"
            logging.exception("falló el lote")   # va a Axiom con su trace_id
            raise
        finally:
            log_run_summary(
                "procesar_lote", component="worker", outcome=outcome,
                duration_ms=(time.perf_counter() - t0) * 1000,
                items=procesados,                # métricas libres
            )
```

**Colas** — el trace continúa del productor al consumidor:

```python
from src.helpers.observability import consumer_span, inject_trace_context

# al encolar, dentro del span del productor
mensaje = {"sku": sku}
inject_trace_context(mensaje)        # agrega el 'traceparent' W3C al mensaje
cola.send(json.dumps(mensaje))

# al consumir
with consumer_span("procesar_sku", carrier=mensaje, attributes={"sku": mensaje["sku"]}):
    ...
```

## La regla de costo (la lección más cara)

**A nivel INFO se exporta SOLO el `run_summary`** — uno por corrida — más todo WARNING/ERROR.
Nunca logs por-ítem (por SKU, por request, por mensaje): en un consumidor de cola son cientos o
miles por corrida y disparan la factura. Esos quedan en consola, no en Axiom. Los spans de alta
frecuencia se muestrean con `ParentBased(root=ratio)` (10% por defecto) para no saturar traces sin
romper la jerarquía del trace.

## Verificar (sin depender de Axiom)

1. **Correlación** — con exportadores in-memory, emitir un `run_summary` dentro de un span y
   confirmar que `log.trace_id == span.trace_id`.
2. **Camino real** — apuntar `AXIOM_LOGS_URL`/`AXIOM_TRACES_URL` a un servidor HTTP local y validar
   el path (`/v1/logs`, `/v1/traces`) y los headers (`Authorization`, `X-Axiom-Dataset`).
3. **No-op** — sin `AXIOM_TOKEN`, el init no hace nada y no falla.

Para Python hay snippets listos en [`telemetria-axiom/python/verificacion.md`](telemetria-axiom/python/verificacion.md).

## Consultar en Axiom (APL)

Los atributos OTLP quedan anidados y **logs y traces mapean distinto** — el detalle completo del
mapeo está en el [`SKILL.md`](telemetria-axiom/SKILL.md).

```kusto
// ¿corrieron los jobs?
['{proyecto}-logs']
| where ['attributes.event.type'] == 'run_summary'
| project _time, ['attributes.job.name'], ['attributes.outcome'], ['attributes.duration_ms'], trace_id
| sort by _time desc

// del trace_id de un error, ver el trace completo
['{proyecto}-traces']
| where trace_id == 'PEGA_EL_TRACE_ID'
| project _time, name, duration, kind, span_id, parent_span_id
| sort by _time asc
```

## Estado de los templates

| Lenguaje | Template | Estado |
|---|---|---|
| Python | [`python/observability.py`](telemetria-axiom/python/observability.py) | ✅ probado end-to-end |
| Node.js / TS | [`nodejs/observability.ts`](telemetria-axiom/nodejs/observability.ts) | ⚠️ validar en un proyecto real |
| C# / .NET | [`csharp/Observability.cs`](telemetria-axiom/csharp/Observability.cs) | ⚠️ validar en un proyecto real |
| Rust | [`rust/observability.rs`](telemetria-axiom/rust/observability.rs) | ⚠️ validar en un proyecto real |

Los cuatro siguen el mismo estándar y las mismas lecciones, con los paquetes y APIs correctos de cada
SDK, pero solo el de Python está corrido y verificado.
