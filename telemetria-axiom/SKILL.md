---
name: telemetria-axiom
description: Implementar telemetría OpenTelemetry → Axiom (logs + traces vía OTLP) en cualquier tipo de servicio — API HTTP, worker de colas, job programado, CLI o función serverless — en Node.js/TS, Python, C# o Rust. Úsala cuando haya que agregar observabilidad — logs estructurados de bajo volumen y traces correlacionados por trace_id — siguiendo el estándar OTel→Axiom (dos datasets {proyecto}-logs / {proyecto}-traces, sin Collector). Trae un template por lenguaje.
---

# Telemetría OpenTelemetry → Axiom (logs + traces)

Agrega observabilidad exportando **logs y traces por OTLP directo a Axiom** (sin Collector). El
estándar es **agnóstico del lenguaje y del tipo de proyecto**: mismos datasets, endpoints, headers,
campos y correlación por `trace_id`. Lo único que cambia por lenguaje es el SDK de OTel y la
sintaxis — por eso hay un **template por lenguaje** que ya resuelve la parte difícil (volumen,
correlación, propagación entre procesos, muestreo, robustez). **No reescribas la lógica desde cero:
copiá el template del lenguaje.**

| Lenguaje | Template | Estado |
|---|---|---|
| Python | `python/observability.py` (+ `python/verificacion.md`) | ✅ probado end-to-end |
| Node.js / TS | `nodejs/observability.ts` | ⚠️ validar en un proyecto real |
| C# / .NET | `csharp/Observability.cs` | ⚠️ validar en un proyecto real |
| Rust | `rust/observability.rs` | ⚠️ validar en un proyecto real |

> Solo el de Python está corrido y verificado acá. Los demás siguen el mismo estándar y las mismas
> lecciones, con los paquetes/APIs correctos de cada SDK, pero hay que **probarlos** (ver §Verificar).

## Cuándo usarla
Cualquier servicio que necesite ver si sus operaciones corren, cuánto tardan y si fallan, y poder
cruzar un log de error con su trace: API HTTP, worker de colas, job programado, CLI, función
serverless. Deploy por contenedor, VM o serverless — da igual.

## El concepto central: la unidad de trabajo

**No pienses en "jobs": pensá en unidades de trabajo.** Una unidad de trabajo es una ejecución de un
punto de entrada. Todo el estándar se define sobre ella — un span por unidad, y como mucho un log de
resumen por unidad. Lo que cambia entre proyectos es **qué es la unidad y con qué frecuencia ocurre**,
y de la frecuencia sale la política de logs (§Control de volumen).

| Tipo de entry point | Unidad de trabajo | `kind` del span | Frecuencia típica | ¿Log de resumen? |
|---|---|---|---|---|
| Job programado / timer / cron | una corrida | `internal` | baja | ✅ uno por corrida |
| CLI / script one-shot | una invocación | `internal` | baja | ✅ uno por invocación |
| Batch / ETL | un lote | `internal` | baja | ✅ uno por lote |
| Consumidor de cola | un mensaje | `consumer` | **alta** | ❌ resumí a nivel del lote/corrida |
| Request HTTP entrante | un request | `server` | **alta** | ❌ solo span + errores |
| Función serverless | una invocación | según trigger | variable | según su frecuencia real |
| Llamada saliente / encolar | una operación | `client` / `producer` | variable | ❌ es un span hijo, no una unidad raíz |

## Prerrequisitos (los provee el dueño de la org de Axiom)
1. Cuenta de Axiom + **dos datasets**: `{proyecto}-logs` y `{proyecto}-traces`.
2. **Token de ingest** (`xaat-…`, solo escritura, scopeado a esos datasets). NO un token personal/admin.
3. Para **ver** en la consola de Axiom hace falta un **usuario/membresía** en la org (el token solo escribe).

## El estándar (igual en todos los lenguajes y proyectos)

### Endpoints y auth (OTLP HTTP)
- Logs:   `POST {AXIOM_LOGS_URL}`   (= `https://api.axiom.co/v1/logs`)
- Traces: `POST {AXIOM_TRACES_URL}` (= `https://api.axiom.co/v1/traces`)
- Headers en AMBOS: `Authorization: Bearer {AXIOM_TOKEN}` y `X-Axiom-Dataset: {dataset}`.

### Variables de entorno (NADA se hardcodea)
| Variable | Ejemplo | Qué es |
|---|---|---|
| `AXIOM_TOKEN` | `xaat-…` | token de ingest |
| `AXIOM_DATASET` | `{proyecto}-logs` | dataset de logs |
| `AXIOM_TRACES_DATASET` | `{proyecto}-traces` | dataset de traces |
| `AXIOM_ENV` | `prod` / `dev` | `deployment.environment` |
| `AXIOM_LOGS_URL` | `https://api.axiom.co/v1/logs` | endpoint de logs |
| `AXIOM_TRACES_URL` | `https://api.axiom.co/v1/traces` | endpoint de traces |
| `OTEL_SERVICE_NAME` | `mi-servicio` | `service.name` (identifica el repo) |
Opcionales: `OTEL_SERVICE_VERSION` (ideal el build id), `OTEL_HIGH_FREQUENCY_SAMPLE_RATE` (default 0.1).
Sin `AXIOM_TOKEN` (o sin las URLs) → **no-op**, la app funciona igual.

### Resource attributes
`service.name` + `deployment.environment` (obligatorios) · `service.version` · `container.id`.

### Campos por log/span
- Logs: `severity`, `body`/`message`, y los atributos del resumen: `event.type=operation_summary`,
  `operation.name`, `component`, `outcome`, `duration_ms` + métricas libres. `trace_id` automático
  cuando el log se emite dentro de un span.
- Spans: `name`, `kind`, `duration`, `status`, `trace_id`/`span_id`. En error: `exception.*`.
- Atributos específicos del tipo de entry point, solo donde aplican: `http.method`/`http.route`/
  `http.status_code` y `request.id` en request/response; `queue.name`/`messaging.message.id` en colas;
  `faas.invocation_id`/`faas.coldstart` en serverless.

## Patrón de uso (mismo en todos los lenguajes)

1. **Init una vez al arranque:** configura los exporters OTLP de logs y traces con los env de arriba.
   Gateado por `AXIOM_TOKEN` (no-op si falta) y a prueba de fallos (no debe tumbar el arranque).
2. **Envolvé cada unidad de trabajo en un span** con el `kind` que le corresponde (tabla de arriba).
   Los errores y el resumen van **DENTRO** del span, así heredan el `trace_id` y se puede cruzar
   log↔trace. (Span afuera, try/catch adentro.)
3. **Un resumen por unidad de trabajo de baja frecuencia:** un ÚNICO log estructurado con
   `event.type=operation_summary`, `operation.name`, `outcome`, `duration_ms` y métricas. Es lo único
   a nivel INFO que se exporta. En unidades de alta frecuencia **no se emite** (§Control de volumen).
4. **Propagación entre procesos:** al **encolar** o llamar a otro servicio, inyectá el `traceparent`
   (W3C) en el mensaje o los headers; al **consumir/recibir**, extraé ese contexto y abrí el span
   continuando el mismo trace.

## Control de volumen y costo (CRÍTICO — la lección más cara)

**La política sale de la frecuencia de la unidad, no del tipo de proyecto.**

- A Axiom se exporta a nivel INFO **SOLO** el resumen de unidades de **baja** frecuencia + **todo
  WARNING/ERROR**. Todo lo demás queda en consola.
- **NUNCA** un log INFO por ítem de alta frecuencia (por SKU / por request / por mensaje): en un
  consumidor de cola o un API con tráfico son cientos o miles por minuto y **disparan el costo**.
- ¿Regla práctica? Si la unidad corre **más de ~1 vez por segundo sostenido**, no emitas resumen por
  unidad: dejá que hablen el span (muestreado) y los errores. Si querés un INFO igual, agregalo un
  nivel más arriba — un resumen por lote, por corrida del consumidor o por ventana de tiempo.
- **Muestreo de traces:** los spans de alta frecuencia se muestrean con `ParentBased(root=ratio)`
  (default 10%) para no saturar traces sin romper la jerarquía del trace. Poné sus nombres en
  `HIGH_FREQUENCY_SPANS`.

## Recetas por tipo de entry point

### Job programado / timer / batch / CLI
La unidad es la corrida: span `internal` + un resumen por corrida. Es el caso directo del template.
En un **CLI** hay una trampa extra: el proceso termina rápido y el exporter es batch — **forzá el
flush antes de salir** o la telemetría de la última corrida se pierde.

### Consumidor de cola (propagación de contexto)
- **Productor:** dentro de su span, inyectá el `traceparent` en el cuerpo del mensaje antes de encolar.
- **Consumidor:** extraé el contexto del mensaje y abrí un span `consumer` que **continúe** el mismo
  trace. Así el trace cruza el borde de la cola y se ve productor → consumidor en una sola vista.
- Es alta frecuencia: el span va en `HIGH_FREQUENCY_SPANS` y **no** lleva resumen por mensaje.

### Serverless / functions (Azure Functions, Lambda)
- **No envuelvas el handler que registra el runtime** con un decorador/wrapper: el SDK lo
  introspecciona (firma, nombre, anotaciones) y se rompe. Envolvé la **llamada interna**.
- **Init una sola vez por proceso**, no por invocación: el runtime reusa el worker entre invocaciones
  y registrar el provider dos veces duplica exporters. Los templates ya chequean si hay uno activo.
- **Flush antes de devolver** si el runtime congela el proceso al terminar la invocación: lo que
  quedó en el batch se pierde o llega tardísimo.
- Marcá el **cold start** como atributo del span de la primera invocación; explica los outliers de
  latencia.

### Otros entry points (HTTP, gRPC, webhooks)
No necesitan receta propia: son el estándar con `kind="server"`, los atributos `http.*` y la política
de alta frecuencia (span muestreado + errores, sin log por request). Si el framework tiene
autoinstrumentación de OTel, usala para los spans de request y dejá el template solo para el init, el
filtro de volumen y los errores.

## Reglas / lecciones aprendidas (no repetir errores)
- **Usá el bridge de logs del SDK de OTel**, no un handler HTTP propio ni SDKs propietarios que
  serializan el log crudo (rompen con excepciones).
- **Correlación:** el resumen y los errores SIEMPRE dentro del span (si no, van sin `trace_id`).
- **Robustez:** init en try/catch y gateado por env; una falla de observabilidad no debe tumbar el
  arranque ni el procesamiento. Los spans no deben tragar excepciones que el código espera.
- **Config 100% por env**, sin defaults quemados de endpoints/dataset/service.name.
- **Nombres de span estables y de baja cardinalidad:** `GET /productos/:id`, no `GET /productos/123`.
  El id va como atributo, nunca en el nombre — si no, el trace se vuelve inconsultable.

## Verificar (por lenguaje, sin depender de Axiom)
1. **Correlación:** con exportadores in-memory, emití un resumen dentro de un span y confirmá que
   `log.trace_id == span.trace_id`.
2. **Camino real:** apuntá `AXIOM_LOGS_URL`/`AXIOM_TRACES_URL` a un servidor HTTP local que capture el
   POST → validá path `/v1/logs`·`/v1/traces` y headers `Authorization`/`X-Axiom-Dataset`.
3. **No-op:** sin `AXIOM_TOKEN`, el init no hace nada ni falla.
(Para Python hay snippets listos en `python/verificacion.md`.)

## Consultar en Axiom (APL) — mapeo real de campos
Los atributos OTLP quedan **anidados**; se referencian con el **path completo entre corchetes**. OJO:
logs y traces mapean distinto.

| | LOGS (`{proyecto}-logs`) | TRACES (`{proyecto}-traces`) |
|---|---|---|
| atributos custom | `['attributes.operation.name']` | `['attributes.custom.<x>']` (¡con `custom`!) |
| environment | `['resource.deployment.environment']` | `['resource.custom.deployment.environment']` |
| service | `['service.name']` | `['service.name']` |
| raíz (ambos) | `trace_id`, `span_id`, `body`, `severity_text` | `trace_id`, `span_id`, `name`, `kind`, `duration` |

- Los atributos **por-ítem** (ej. `sku`, `http.route`) existen solo en algunos spans → proyectalos
  **después** de filtrar por el `name` del span que los emite.

Ejemplos:
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

// latencia por unidad de trabajo (sirve igual para rutas HTTP, colas o jobs)
['{proyecto}-traces']
| where isnotnull(name)
| summarize p50 = percentile(duration, 50), p95 = percentile(duration, 95), n = count() by name, kind
| sort by p95 desc
```
