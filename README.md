# claude-skills

Skills para [Claude Code](https://claude.com/claude-code). Cada carpeta es una skill autocontenida:
un `SKILL.md` con el estándar y las lecciones aprendidas, más los templates que Claude copia y adapta
al proyecto en el que estés trabajando.

| Skill | Qué hace | Lenguajes |
|---|---|---|
| [`telemetria-axiom/`](telemetria-axiom/) | Telemetría OpenTelemetry → Axiom: logs estructurados + traces correlacionados por `trace_id`, vía OTLP directo (sin Collector). Agnóstica del tipo de proyecto | Python · Node.js/TS · C# · Rust |

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

Cualquier servicio en el que no podés ver si sus operaciones corren, cuánto tardan y si fallan: API
HTTP, worker de colas, job programado, CLI o función serverless. La skill lo instrumenta para que
exporte a Axiom:

- **Logs** — un resumen estructurado por unidad de trabajo (`event.type=operation_summary`) + todo
  WARNING/ERROR.
- **Traces** — un span por unidad de trabajo, con propagación de contexto entre procesos.
- **Correlación** — el `trace_id` cruza ambos datasets: de un log de error saltás al trace completo.

Todo por **OTLP directo a Axiom, sin Collector**, y **100% configurado por variables de entorno**:
sin `AXIOM_TOKEN` la instrumentación es no-op y la app corre igual.

### El concepto: la unidad de trabajo

La skill no asume que estés instrumentando un job. Una **unidad de trabajo** es una ejecución de un
punto de entrada, y todo el estándar se define sobre ella: un span por unidad, y como mucho un log de
resumen por unidad. Lo que cambia entre proyectos es qué es la unidad y con qué frecuencia ocurre —
de ahí sale la política de logs.

| Tipo de entry point | Unidad de trabajo | `kind` | Frecuencia | ¿Log de resumen? |
|---|---|---|---|---|
| Job programado / timer | una corrida | `internal` | baja | ✅ uno por corrida |
| CLI / script one-shot | una invocación | `internal` | baja | ✅ uno por invocación |
| Batch / ETL | un lote | `internal` | baja | ✅ uno por lote |
| Consumidor de cola | un mensaje | `consumer` | **alta** | ❌ resumí a nivel del lote |
| Request HTTP entrante | un request | `server` | **alta** | ❌ solo span + errores |
| Función serverless | una invocación | según trigger | variable | según su frecuencia real |

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
2. Identifica cuáles son las unidades de trabajo del proyecto y con qué frecuencia corren.
3. Ajusta la config del template: el scope de instrumentación y qué spans son de alta frecuencia.
4. Agrega las dependencias de OTel al manifiesto (`requirements.txt`, `package.json`, `.csproj`, `Cargo.toml`).
5. Llama a `setup_observability()` una vez al arranque.
6. Envuelve cada unidad de trabajo en un span con su `kind`, con el resumen y los errores **adentro**.
7. Propaga el `traceparent` entre procesos: al encolar o llamar, y lo extrae al consumir o recibir.
8. Te deja las consultas APL listas para pegar en Axiom.

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

**Una unidad de baja frecuencia** — span afuera, try/except adentro, para que el resumen y los
errores hereden el `trace_id`:

```python
from src.helpers.observability import work_span, log_summary

def procesar_lote():
    t0 = time.perf_counter()
    with work_span("procesar-lote", kind="internal"):
        procesados, outcome = 0, "success"
        try:
            procesados = hacer_trabajo()
        except Exception:
            outcome = "error"
            logging.exception("falló el lote")   # va a Axiom con su trace_id
            raise
        finally:
            log_summary(
                "procesar-lote", component="etl", outcome=outcome,
                duration_ms=(time.perf_counter() - t0) * 1000,
                items=procesados,                # métricas libres
            )
```

**Una unidad de alta frecuencia** — span con su `kind`, sin log de resumen: el span ya mide, y un
log por request o por mensaje es lo que dispara la factura.

```python
with work_span("GET /productos/:id", kind="server",
               attributes={"http.method": "GET", "http.route": "/productos/:id"}):
    ...
```

Ojo con el nombre del span: `GET /productos/:id`, nunca `GET /productos/123` — el id va como
atributo. Un nombre de alta cardinalidad vuelve el trace inconsultable.

**Propagación entre procesos** — el trace continúa del productor al consumidor:

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

**La política sale de la frecuencia de la unidad, no del tipo de proyecto.** A nivel INFO se exporta
solo el resumen de las unidades de **baja** frecuencia, más todo WARNING/ERROR. Nunca logs por-ítem
de alta frecuencia (por SKU, por request, por mensaje): en un consumidor de cola o un API con tráfico
son cientos o miles por minuto y disparan la factura. Esos quedan en consola, no en Axiom.

Regla práctica: si la unidad corre **más de ~1 vez por segundo sostenido**, no emitas resumen por
unidad — dejá que hablen el span y los errores, y si querés un INFO igual, ponelo un nivel más arriba
(por lote o por ventana de tiempo). Los spans de alta frecuencia se muestrean con
`ParentBased(root=ratio)` (10% por defecto) para no saturar traces sin romper la jerarquía del trace.

## Verificar (sin depender de Axiom)

1. **Correlación** — con exportadores in-memory, emitir un `operation_summary` dentro de un span y
   confirmar que `log.trace_id == span.trace_id`.
2. **Camino real** — apuntar `AXIOM_LOGS_URL`/`AXIOM_TRACES_URL` a un servidor HTTP local y validar
   el path (`/v1/logs`, `/v1/traces`) y los headers (`Authorization`, `X-Axiom-Dataset`).
3. **No-op** — sin `AXIOM_TOKEN`, el init no hace nada y no falla.

Para Python hay snippets listos en [`telemetria-axiom/python/verificacion.md`](telemetria-axiom/python/verificacion.md).

## Consultar en Axiom (APL)

Los atributos OTLP quedan anidados y **logs y traces mapean distinto** — el detalle completo del
mapeo está en el [`SKILL.md`](telemetria-axiom/SKILL.md).

```kusto
// ¿corrieron las operaciones, y cómo les fue?
['{proyecto}-logs']
| where ['attributes.event.type'] == 'operation_summary'
| project _time, ['attributes.operation.name'], ['attributes.outcome'], ['attributes.duration_ms'], trace_id
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
