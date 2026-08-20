---
name: telemetria-axiom
description: Implementar telemetría OpenTelemetry → Axiom (logs + traces vía OTLP) en un servicio, en Node.js/TS, Python, C# o Rust. Úsala cuando haya que agregar observabilidad — logs estructurados de bajo volumen y traces correlacionados por trace_id — siguiendo el estándar OTel→Axiom (dos datasets {proyecto}-logs / {proyecto}-traces, sin Collector). Trae un template por lenguaje.
---

# Telemetría OpenTelemetry → Axiom (logs + traces)

Agrega observabilidad exportando **logs y traces por OTLP directo a Axiom** (sin Collector). El
estándar es **agnóstico del lenguaje**: mismos datasets, endpoints, headers, campos y correlación por
`trace_id`. Lo único que cambia por lenguaje es el SDK de OTel y la sintaxis — por eso hay un
**template por lenguaje** que ya resuelve la parte difícil (volumen, correlación, propagación en
colas, muestreo, robustez). **No reescribas la lógica desde cero: copiá el template del lenguaje.**

| Lenguaje | Template | Estado |
|---|---|---|
| Python | `python/observability.py` (+ `python/verificacion.md`) | ✅ probado end-to-end |
| Node.js / TS | `nodejs/observability.ts` | ⚠️ validar en un proyecto real |
| C# / .NET | `csharp/Observability.cs` | ⚠️ validar en un proyecto real |
| Rust | `rust/observability.rs` | ⚠️ validar en un proyecto real |

> Solo el de Python está corrido y verificado acá. Los demás siguen el mismo estándar y las mismas
> lecciones, con los paquetes/APIs correctos de cada SDK, pero hay que **probarlos** (ver §Verificar).

## Cuándo usarla
Un servicio/worker (timers, colas, HTTP) que necesita ver si sus operaciones corren, cuánto tardan y
si fallan, y poder cruzar un log de error con su trace. Deploy por contenedor o serverless — da igual.

## Prerrequisitos (los provee el dueño de la org de Axiom)
1. Cuenta de Axiom + **dos datasets**: `{proyecto}-logs` y `{proyecto}-traces`.
2. **Token de ingest** (`xaat-…`, solo escritura, scopeado a esos datasets). NO un token personal/admin.
3. Para **ver** en la consola de Axiom hace falta un **usuario/membresía** en la org (el token solo escribe).

## El estándar (igual en todos los lenguajes)

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

### Resource attributes (§7.1 del estándar)
`service.name` + `deployment.environment` (obligatorios) · `service.version` · `container.id`.

### Campos por log/span
- Logs: `severity`, `body`/`message`, y los atributos del `run_summary` (ver abajo). `trace_id`
  automático cuando el log se emite dentro de un span.
- Spans: `name`, `kind` (`internal`/`consumer`/`server`/`producer`/`client`), `duration`, `status`,
  `trace_id`/`span_id`. En error: `exception.*`.
- Campos HTTP del estándar (`http.method/route/status_code`, `request.id`) aplican solo a servicios
  request/response; en workers de timers/colas se omiten.

## Patrón de uso (mismo en todos los lenguajes)

1. **Init una vez al arranque:** configura los exporters OTLP de logs y traces con los env de arriba.
   Gateado por `AXIOM_TOKEN` (no-op si falta) y a prueba de fallos (no debe tumbar el arranque).
2. **Un "run summary" por corrida de cada entry point:** un ÚNICO log estructurado por ejecución con
   `event.type=run_summary`, `job.name`, `outcome`, `duration_ms` y métricas. Es lo único a nivel INFO
   que se exporta.
3. **Envolvé cada entry point en un span, con el `run_summary` y los errores DENTRO del span** — así
   heredan el `trace_id` y se puede cruzar log↔trace. (Span afuera, try/catch adentro.)
4. **Colas — propagación de contexto:** al **encolar**, inyectá el `traceparent` (W3C) en el mensaje;
   al **consumir**, extraé ese contexto y abrí un span `consumer` que continúe el mismo trace.

## Control de volumen y costo (CRÍTICO — la lección más cara)
- A Axiom se exporta a nivel INFO **SOLO** el `run_summary` (uno por corrida) + **todo WARNING/ERROR**.
- **NUNCA** mandes logs por-ítem (por-SKU / por-request / por-mensaje) a INFO: en un consumidor de cola
  son cientos/miles por corrida y **disparan el costo**. Quedan en consola, no en Axiom.
- **Muestreo de traces:** los spans de alta frecuencia (consumidores por-mensaje) se muestrean con
  `ParentBased(root=ratio)` (default 10%) para no saturar traces sin romper la jerarquía del trace.

## Reglas / lecciones aprendidas (no repetir errores)
- **Usá el bridge de logs del SDK de OTel**, no un handler HTTP propio ni SDKs propietarios que
  serializan el log crudo (rompen con excepciones).
- **Correlación:** el `run_summary` y los errores SIEMPRE dentro del span (si no, van sin `trace_id`).
- **Robustez:** init en try/catch y gateado por env; una falla de observabilidad no debe tumbar el
  arranque ni el procesamiento. Los spans no deben tragar excepciones que el código espera.
- **En serverless con handlers registrados por el runtime** (ej. Azure Functions): no envuelvas el
  handler con un decorador/wrapper que el runtime introspecciona — envolvé la **llamada interna**.
- **Config 100% por env**, sin defaults quemados de endpoints/dataset/service.name.

## Verificar (por lenguaje, sin depender de Axiom)
1. **Correlación:** con exportadores in-memory, emití un `run_summary` dentro de un span y confirmá
   que `log.trace_id == span.trace_id`.
2. **Camino real:** apuntá `AXIOM_LOGS_URL`/`AXIOM_TRACES_URL` a un servidor HTTP local que capture el
   POST → validá path `/v1/logs`·`/v1/traces` y headers `Authorization`/`X-Axiom-Dataset`.
3. **No-op:** sin `AXIOM_TOKEN`, el init no hace nada ni falla.
(Para Python hay snippets listos en `python/verificacion.md`.)

## Consultar en Axiom (APL) — mapeo real de campos
Los atributos OTLP quedan **anidados**; se referencian con el **path completo entre corchetes**. OJO:
logs y traces mapean distinto.

| | LOGS (`{proyecto}-logs`) | TRACES (`{proyecto}-traces`) |
|---|---|---|
| atributos custom | `['attributes.job.name']` | `['attributes.custom.<x>']` (¡con `custom`!) |
| environment | `['resource.deployment.environment']` | `['resource.custom.deployment.environment']` |
| service | `['service.name']` | `['service.name']` |
| raíz (ambos) | `trace_id`, `span_id`, `body`, `severity_text` | `trace_id`, `span_id`, `name`, `kind`, `duration` |

- Los atributos **por-ítem** (ej. `sku`) existen solo en algunos spans → proyectalos **después** de
  filtrar por el `name` del span que los emite.

Ejemplos:
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
