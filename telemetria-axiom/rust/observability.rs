//! Observabilidad: logs + traces a Axiom vía OpenTelemetry (OTLP). Rust.
//! ===================================================================
//! Estándar OTel→Axiom: dos datasets {proyecto}-logs / {proyecto}-traces, sin Collector. Agnóstico
//! del tipo de proyecto: API HTTP, worker de colas, job programado, CLI o función serverless. Lo
//! único que cambia es cuál es la UNIDAD DE TRABAJO (un request, un mensaje, una corrida, una
//! invocación) y con qué frecuencia ocurre.
//! Idiomático en Rust: se usa `tracing` para spans y eventos; capas de `tracing-opentelemetry` y
//! `opentelemetry-appender-tracing` exportan a OTLP. Los eventos emitidos dentro de un span heredan
//! el trace_id automáticamente (correlación).
//!
//! Cargo.toml (PINEAR versiones compatibles entre sí — la API de OTel Rust cambia seguido):
//!   opentelemetry = { version = "0.24", features = ["trace", "logs"] }
//!   opentelemetry_sdk = { version = "0.24", features = ["rt-tokio", "trace", "logs"] }
//!   opentelemetry-otlp = { version = "0.17", features = ["http-proto", "reqwest-client", "logs"] }
//!   tracing = "0.1"
//!   tracing-subscriber = { version = "0.3", features = ["env-filter"] }
//!   tracing-opentelemetry = "0.25"
//!   opentelemetry-appender-tracing = "0.5"
//!
//! ⚠️ Scaffold a validar/pinear en un proyecto real (el de Python es el probado end-to-end).

use std::collections::HashMap;
use std::env;

use opentelemetry::{global, trace::TracerProvider as _, KeyValue};
use opentelemetry::propagation::{Injector, Extractor};
use opentelemetry_sdk::{
    logs::LoggerProvider,
    trace::{self as sdktrace, Sampler},
    Resource, runtime,
};
use opentelemetry_otlp::WithExportConfig;
use tracing_subscriber::prelude::*;

// --- CONFIG DEL PROYECTO (ajustar) -------------------------------------------------------------
const INSTRUMENTATION_SCOPE: &str = "observability";
/// Spans de alta frecuencia a muestrear: rutas HTTP con tráfico, consumidores por-mensaje, handlers
/// serverless calientes. Vacío = exportar todos.
const HIGH_FREQUENCY_SPANS: &[&str] = &[/* "process_queue_message" */];
/// Target dedicado del resumen de la unidad de trabajo (filtro de volumen: solo esto + WARN/ERROR
/// van a OTLP).
pub const SUMMARY_TARGET: &str = "operation_summary";

fn opt(name: &str) -> Option<String> { env::var(name).ok() }

fn sample_rate() -> f64 {
    opt("OTEL_HIGH_FREQUENCY_SAMPLE_RATE").and_then(|s| s.parse().ok()).unwrap_or(0.1).clamp(0.0, 1.0)
}

fn resource() -> Resource {
    let mut kvs = vec![KeyValue::new("deployment.environment", opt("AXIOM_ENV").unwrap_or_else(|| "unknown".into()))];
    if let Some(s) = opt("OTEL_SERVICE_NAME") { kvs.push(KeyValue::new("service.name", s)); }
    if let Some(v) = opt("OTEL_SERVICE_VERSION") { kvs.push(KeyValue::new("service.version", v)); }
    if let Some(c) = opt("HOSTNAME") { kvs.push(KeyValue::new("container.id", c)); }
    Resource::new(kvs)
}

fn axiom_headers(token: &str, dataset: &str) -> HashMap<String, String> {
    HashMap::from([
        ("Authorization".into(), format!("Bearer {token}")),
        ("X-Axiom-Dataset".into(), dataset.into()),
    ])
}

/// Muestrea los spans de alta frecuencia; el resto siempre. ParentBased para no romper el trace.
fn selective_sampler() -> Sampler {
    let rate = sample_rate();
    // Nota: OTel Rust no trae un "sampler por nombre" listo; si necesitás muestreo selectivo por
    // nombre de span, implementá `ShouldSample` custom. Como base, ParentBased(TraceIdRatio) global:
    if HIGH_FREQUENCY_SPANS.is_empty() {
        Sampler::ParentBased(Box::new(Sampler::AlwaysOn))
    } else {
        Sampler::ParentBased(Box::new(Sampler::TraceIdRatioBased(rate)))
    }
}

/// Configura logs y traces a Axiom si hay credenciales. No hace panic (no debe tumbar el arranque).
/// Devuelve los providers: mantenelos vivos y llamá shutdown al terminar.
pub fn init_observability() -> Option<(sdktrace::TracerProvider, LoggerProvider)> {
    let token = opt("AXIOM_TOKEN")?; // no-op si falta

    // --- Traces ---
    let tracer_provider = match (opt("AXIOM_TRACES_DATASET"), opt("AXIOM_TRACES_URL")) {
        (Some(ds), Some(url)) => {
            let exporter = opentelemetry_otlp::SpanExporter::builder()
                .with_http().with_endpoint(url).with_headers(axiom_headers(&token, &ds))
                .build().ok()?;
            let tp = sdktrace::TracerProvider::builder()
                .with_batch_exporter(exporter, runtime::Tokio)
                .with_config(sdktrace::Config::default().with_resource(resource()).with_sampler(selective_sampler()))
                .build();
            global::set_tracer_provider(tp.clone());
            let otel_layer = tracing_opentelemetry::layer().with_tracer(tp.tracer(INSTRUMENTATION_SCOPE));
            // (registrá `otel_layer` en tu subscriber — ver más abajo)
            Some((tp, otel_layer))
        }
        _ => None,
    };

    // --- Logs ---
    let logger_provider = match (opt("AXIOM_DATASET"), opt("AXIOM_LOGS_URL")) {
        (Some(ds), Some(url)) => {
            let exporter = opentelemetry_otlp::LogExporter::builder()
                .with_http().with_endpoint(url).with_headers(axiom_headers(&token, &ds))
                .build().ok()?;
            Some(LoggerProvider::builder()
                .with_batch_exporter(exporter, runtime::Tokio)
                .with_resource(resource())
                .build())
        }
        _ => None,
    };

    // --- Subscriber de tracing: OTel traces + bridge de logs + filtro de volumen ---
    // Volumen: a OTLP solo el target SUMMARY_TARGET (info) + WARN/ERROR del resto.
    let filter = tracing_subscriber::EnvFilter::new(format!("warn,{SUMMARY_TARGET}=info"));
    let registry = tracing_subscriber::registry().with(filter);
    // registry.with(tracer_provider.1 otel_layer).with(OpenTelemetryTracingBridge::new(&logger_provider)) ...
    // (armá el subscriber combinando las capas disponibles y hacé `.init()` una sola vez)
    let _ = registry; // completar según las capas activas

    match (tracer_provider, logger_provider) {
        (Some((tp, _)), Some(lp)) => Some((tp, lp)),
        _ => None,
    }
}

// --- Patrones de uso (con `tracing`) -----------------------------------------------------------

/// UN evento por unidad de trabajo, SOLO para unidades de BAJA frecuencia (job, corrida de CLI,
/// lote). Emitilo DENTRO de un span para heredar el trace_id. En unidades de ALTA frecuencia
/// (por-request, por-mensaje) no lo emitas: alcanzan el span muestreado y los errores.
///
/// `otel.kind`: "server" (request HTTP entrante), "consumer" (mensaje de cola), "producer"
/// (encolar), "client" (llamada saliente), "internal" (job, CLI, cómputo).
///
/// Ejemplo de uso (macro `tracing::info!` con el target dedicado y campos estructurados):
///
///   let span = tracing::info_span!("mi-operacion", otel.kind = "internal");
///   let _g = span.enter();
///   // ... trabajo ...
///   tracing::info!(target: SUMMARY_TARGET,
///       event.type = "operation_summary", operation.name = "mi-operacion", outcome = "success",
///       duration_ms = elapsed_ms, procesados = n);
///
/// Errores (siempre se exportan): `tracing::error!(operation.name = "mi-operacion", error = %e, "…");`
///
/// Colas — propagación:
///   Productor: inyectar el contexto del span activo en el mensaje antes de encolar:
///       let mut carrier = HashMap::new();
///       inject_current_context(&mut carrier);
///   Consumidor: abrir el span con el contexto extraído:
///       let parent = extract_context(&carrier);
///       let span = tracing::info_span!("MiConsumo", otel.kind = "consumer");
///       span.set_parent(parent);   // de tracing_opentelemetry::OpenTelemetrySpanExt
///       let _g = span.enter();

/// Inyecta el traceparent del contexto activo en un carrier (HashMap del mensaje).
pub fn inject_current_context(carrier: &mut HashMap<String, String>) {
    use tracing_opentelemetry::OpenTelemetrySpanExt;
    let cx = tracing::Span::current().context();
    global::get_text_map_propagator(|prop| prop.inject_context(&cx, &mut HeaderInjector(carrier)));
}

/// Extrae el contexto propagado desde un carrier (para set_parent en el span consumidor).
pub fn extract_context(carrier: &HashMap<String, String>) -> opentelemetry::Context {
    global::get_text_map_propagator(|prop| prop.extract(&HeaderExtractor(carrier)))
}

struct HeaderInjector<'a>(&'a mut HashMap<String, String>);
impl<'a> Injector for HeaderInjector<'a> {
    fn set(&mut self, key: &str, value: String) { self.0.insert(key.to_string(), value); }
}
struct HeaderExtractor<'a>(&'a HashMap<String, String>);
impl<'a> Extractor for HeaderExtractor<'a> {
    fn get(&self, key: &str) -> Option<&str> { self.0.get(key).map(|s| s.as_str()) }
    fn keys(&self) -> Vec<&str> { self.0.keys().map(|s| s.as_str()).collect() }
}
