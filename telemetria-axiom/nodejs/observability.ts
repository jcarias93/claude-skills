/**
 * Observabilidad: logs + traces a Axiom vía OpenTelemetry (OTLP). Node.js / TypeScript.
 * =================================================================================
 * Estándar OTel→Axiom: dos datasets {proyecto}-logs / {proyecto}-traces, sin Collector.
 *
 * Agnóstico del tipo de proyecto: API HTTP, worker de colas, job programado, CLI o función
 * serverless. Lo único que cambia es cuál es la UNIDAD DE TRABAJO (un request, un mensaje, una
 * corrida, una invocación) y con qué frecuencia ocurre.
 *
 * Enfoque de VOLUMEN: NO se bridea todo el logger de la app a Axiom (eso satura y cuesta). Solo se
 * exportan, vía la Logs API de OTel, (a) un `logSummary` por unidad de trabajo de BAJA frecuencia y
 * (b) warnings/errores con `logEvent`. El logging normal (pino/console) queda local. En unidades de
 * ALTA frecuencia (por-request, por-mensaje) no se emite resumen: alcanzan el span muestreado y los
 * errores.
 *
 * Paquetes:
 *   @opentelemetry/api @opentelemetry/api-logs @opentelemetry/resources
 *   @opentelemetry/semantic-conventions @opentelemetry/sdk-logs
 *   @opentelemetry/exporter-logs-otlp-http @opentelemetry/sdk-trace-node
 *   @opentelemetry/exporter-trace-otlp-http
 *
 * ⚠️ Template a validar en un proyecto real (el de Python es el probado end-to-end).
 */
import { trace, context, propagation, SpanKind, SpanStatusCode, Span } from '@opentelemetry/api';
import { logs, SeverityNumber } from '@opentelemetry/api-logs';
import { Resource } from '@opentelemetry/resources';
import { SemanticResourceAttributes } from '@opentelemetry/semantic-conventions';
import { LoggerProvider, BatchLogRecordProcessor } from '@opentelemetry/sdk-logs';
import { OTLPLogExporter } from '@opentelemetry/exporter-logs-otlp-http';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { BatchSpanProcessor, ParentBasedSampler, TraceIdRatioBasedSampler, AlwaysOnSampler, Sampler } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';

// --- CONFIG DEL PROYECTO (ajustar) -------------------------------------------------------------
const INSTRUMENTATION_SCOPE = 'observability';
// Spans de alta frecuencia a muestrear: rutas HTTP con tráfico, consumidores por-mensaje, handlers
// serverless calientes. Vacío = exportar todos.
const HIGH_FREQUENCY_SPANS = new Set<string>([/* 'ProcessQueueMessage' */]);
const SUMMARY_EVENT = 'operation_summary';

function env(name: string): string | undefined { return process.env[name]; }
function sampleRate(): number {
  const r = parseFloat(env('OTEL_HIGH_FREQUENCY_SAMPLE_RATE') || '0.1');
  return Number.isFinite(r) ? Math.min(1, Math.max(0, r)) : 0.1;
}

function resource(): Resource {
  const attrs: Record<string, string> = {
    [SemanticResourceAttributes.DEPLOYMENT_ENVIRONMENT]: env('AXIOM_ENV') || 'unknown',
  };
  const svc = env('OTEL_SERVICE_NAME');
  if (svc) attrs[SemanticResourceAttributes.SERVICE_NAME] = svc;
  const ver = env('OTEL_SERVICE_VERSION');
  if (ver) attrs[SemanticResourceAttributes.SERVICE_VERSION] = ver;
  if (env('HOSTNAME')) attrs[SemanticResourceAttributes.CONTAINER_ID] = env('HOSTNAME')!;
  return new Resource(attrs);
}

class SelectiveRootSampler implements Sampler {
  private ratio: Sampler;
  private always = new AlwaysOnSampler();
  constructor(rate: number) { this.ratio = new TraceIdRatioBasedSampler(rate); }
  shouldSample(ctx: any, traceId: string, name: string, kind: any, attrs: any, links: any) {
    const s = HIGH_FREQUENCY_SPANS.has(name) ? this.ratio : this.always;
    return s.shouldSample(ctx, traceId, name, kind, attrs, links);
  }
  toString() { return 'SelectiveRootSampler'; }
}

let _tracesReady = false;

/** Configura logs y traces a Axiom si hay credenciales. No lanza (no debe tumbar el arranque). */
export function initObservability(): void {
  const token = env('AXIOM_TOKEN');
  if (!token) return; // no-op

  // --- Logs ---
  const logsDataset = env('AXIOM_DATASET');
  const logsUrl = env('AXIOM_LOGS_URL');
  if (logsDataset && logsUrl) {
    try {
      const provider = new LoggerProvider({ resource: resource() });
      provider.addLogRecordProcessor(new BatchLogRecordProcessor(new OTLPLogExporter({
        url: logsUrl,
        headers: { Authorization: `Bearer ${token}`, 'X-Axiom-Dataset': logsDataset },
      })));
      logs.setGlobalLoggerProvider(provider);
    } catch (e) { console.error('[obs] setup de logs falló:', e); }
  } else if (logsDataset && !logsUrl) {
    console.error('[obs] AXIOM_LOGS_URL no seteada -> logs deshabilitados');
  }

  // --- Traces ---
  const tracesDataset = env('AXIOM_TRACES_DATASET');
  const tracesUrl = env('AXIOM_TRACES_URL');
  if (tracesDataset && tracesUrl) {
    try {
      const provider = new NodeTracerProvider({
        resource: resource(),
        sampler: new ParentBasedSampler({ root: new SelectiveRootSampler(sampleRate()) }),
      });
      provider.addSpanProcessor(new BatchSpanProcessor(new OTLPTraceExporter({
        url: tracesUrl,
        headers: { Authorization: `Bearer ${token}`, 'X-Axiom-Dataset': tracesDataset },
      })));
      provider.register(); // registra el context manager (async hooks) y el propagador W3C
      _tracesReady = true;
    } catch (e) { console.error('[obs] setup de traces falló:', e); }
  } else if (tracesDataset && !tracesUrl) {
    console.error('[obs] AXIOM_TRACES_URL no seteada -> traces deshabilitados');
  }
}

// --- Logs estructurados a Axiom (lo ÚNICO que se exporta a nivel info) ---------------------------

/** UN log por unidad de trabajo, solo para unidades de BAJA frecuencia (job, corrida de CLI, lote).
 *  Llamalo DENTRO de workSpan() para que herede el trace_id. En unidades de alta frecuencia
 *  (por-request, por-mensaje) NO lo llames: dispara el costo y el span ya mide. */
export function logSummary(
  operation: string, component: string, outcome: string, durationMs: number,
  extra: Record<string, string | number | boolean> = {},
): void {
  const attributes: Record<string, any> = {
    'event.type': SUMMARY_EVENT, 'operation.name': operation, component, outcome,
    duration_ms: Math.round(durationMs * 100) / 100, ...extra,
  };
  logs.getLogger(INSTRUMENTATION_SCOPE).emit({
    severityNumber: SeverityNumber.INFO, severityText: 'INFO',
    body: `[SUMMARY] ${operation} outcome=${outcome} duration_ms=${durationMs.toFixed(2)}`,
    attributes,
  });
}

/** Warnings/errores (siempre se exportan). Emitilo dentro del span para heredar el trace_id. */
export function logEvent(severity: 'WARN' | 'ERROR', message: string, attributes: Record<string, any> = {}): void {
  logs.getLogger(INSTRUMENTATION_SCOPE).emit({
    severityNumber: severity === 'ERROR' ? SeverityNumber.ERROR : SeverityNumber.WARN,
    severityText: severity, body: message, attributes,
  });
}

// --- Instrumentación (no-op si traces no está configurado) --------------------------------------

const KIND: Record<string, SpanKind> = {
  internal: SpanKind.INTERNAL, server: SpanKind.SERVER,
  consumer: SpanKind.CONSUMER, producer: SpanKind.PRODUCER, client: SpanKind.CLIENT,
};

/** Ejecuta `fn` dentro del span de una unidad de trabajo. `kind`: 'server' (request HTTP entrante),
 *  'consumer' (mensaje de cola), 'producer' (encolar), 'client' (llamada saliente), 'internal' (job,
 *  CLI, cómputo). Registra excepción + status ERROR y la RE-LANZA. Envolvé con esto el cuerpo del
 *  entry point (con logSummary/errores adentro). */
export async function workSpan<T>(name: string, kind: keyof typeof KIND, fn: (span: Span) => Promise<T> | T): Promise<T> {
  if (!_tracesReady) return await fn(undefined as any);
  const tracer = trace.getTracer(INSTRUMENTATION_SCOPE);
  return await tracer.startActiveSpan(name, { kind: KIND[kind] ?? SpanKind.INTERNAL }, async (span) => {
    try {
      return await fn(span);
    } catch (e: any) {
      span.recordException(e);
      span.setStatus({ code: SpanStatusCode.ERROR, message: String(e?.message ?? e) });
      throw e;
    } finally {
      span.end();
    }
  });
}

/** Span CONSUMER que continúa el trace propagado en un mensaje de cola (carrier con `traceparent`). */
export async function consumerSpan<T>(name: string, carrier: Record<string, any> | undefined, fn: (span: Span) => Promise<T> | T): Promise<T> {
  if (!_tracesReady) return await fn(undefined as any);
  const parent = carrier ? propagation.extract(context.active(), carrier) : context.active();
  const tracer = trace.getTracer(INSTRUMENTATION_SCOPE);
  return await context.with(parent, () =>
    tracer.startActiveSpan(name, { kind: SpanKind.CONSUMER }, async (span) => {
      try { return await fn(span); }
      catch (e: any) { span.recordException(e); span.setStatus({ code: SpanStatusCode.ERROR }); throw e; }
      finally { span.end(); }
    }),
  );
}

/** Inyecta el `traceparent` del span activo en el dict del mensaje (llamalo antes de encolar). */
export function injectTraceContext<T extends Record<string, any>>(carrier: T): T {
  try { propagation.inject(context.active(), carrier); } catch { /* no-op */ }
  return carrier;
}
