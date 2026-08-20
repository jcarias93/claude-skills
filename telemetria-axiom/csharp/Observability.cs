// Observabilidad: logs + traces a Axiom vía OpenTelemetry (OTLP). C# / .NET 6+.
// ============================================================================
// Estándar OTel→Axiom: dos datasets {proyecto}-logs / {proyecto}-traces, sin Collector.
//
// Paquetes (NuGet):
//   OpenTelemetry
//   OpenTelemetry.Exporter.OpenTelemetryProtocol
//   Microsoft.Extensions.Logging
//
// Agnóstico del tipo de proyecto: API HTTP, worker de colas, job programado, CLI o función
// serverless. Lo único que cambia es cuál es la UNIDAD DE TRABAJO (un request, un mensaje, una
// corrida, una invocación) y con qué frecuencia ocurre.
//
// VOLUMEN: el exporter de logs se configura con nivel mínimo Warning + una categoría dedicada para
// el resumen (Information). Así a Axiom van solo los resúmenes y los warnings/errores; el resto del
// logging de la app queda local. Ajustá el filtro en Init según tu app. El resumen es para unidades
// de BAJA frecuencia; en las de ALTA frecuencia (por-request, por-mensaje) alcanzan el span
// muestreado y los errores.
//
// ⚠️ Template a validar en un proyecto real (el de Python es el probado end-to-end).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
using OpenTelemetry.Logs;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

public static class Observability
{
    // --- CONFIG DEL PROYECTO (ajustar) ---------------------------------------------------------
    private const string InstrumentationScope = "Observability";
    // Spans de alta frecuencia a muestrear: rutas HTTP con tráfico, consumidores por-mensaje,
    // handlers serverless calientes. Vacío = exportar todos.
    private static readonly HashSet<string> HighFrequencySpans = new() { /* "ProcessQueueMessage" */ };

    public static readonly ActivitySource Source = new(InstrumentationScope);
    private static TracerProvider? _tracer;
    private static ILoggerFactory? _loggerFactory;
    public static ILogger SummaryLogger { get; private set; } = new NullLogger();
    private static readonly TextMapPropagator Propagator = Propagators.DefaultTextMapPropagator;

    private static string? Env(string n) => Environment.GetEnvironmentVariable(n);

    private static double SampleRate()
        => double.TryParse(Env("OTEL_HIGH_FREQUENCY_SAMPLE_RATE"), out var r) ? Math.Clamp(r, 0, 1) : 0.1;

    private static ResourceBuilder BuildResource()
    {
        var attrs = new List<KeyValuePair<string, object>>
        {
            new("deployment.environment", Env("AXIOM_ENV") ?? "unknown"),
        };
        if (Env("OTEL_SERVICE_VERSION") is { } v) attrs.Add(new("service.version", v));
        if (Env("HOSTNAME") is { } c) attrs.Add(new("container.id", c));
        var rb = ResourceBuilder.CreateDefault().AddAttributes(attrs);
        if (Env("OTEL_SERVICE_NAME") is { } svc) rb.AddService(svc);
        return rb;
    }

    /// <summary>Configura logs y traces a Axiom si hay credenciales. No lanza (no debe tumbar el arranque).</summary>
    public static void Init()
    {
        var token = Env("AXIOM_TOKEN");
        if (string.IsNullOrEmpty(token)) return; // no-op

        // --- Traces ---
        var tracesDs = Env("AXIOM_TRACES_DATASET");
        var tracesUrl = Env("AXIOM_TRACES_URL");
        if (!string.IsNullOrEmpty(tracesDs) && !string.IsNullOrEmpty(tracesUrl))
        {
            try
            {
                _tracer = Sdk.CreateTracerProviderBuilder()
                    .AddSource(InstrumentationScope)
                    .SetResourceBuilder(BuildResource())
                    .SetSampler(new ParentBasedSampler(new SelectiveRootSampler(SampleRate())))
                    .AddOtlpExporter(o =>
                    {
                        o.Endpoint = new Uri(tracesUrl!);
                        o.Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf;
                        o.Headers = $"Authorization=Bearer {token},X-Axiom-Dataset={tracesDs}";
                    })
                    .Build();
            }
            catch (Exception e) { Console.Error.WriteLine($"[obs] setup de traces falló: {e}"); }
        }

        // --- Logs ---
        var logsDs = Env("AXIOM_DATASET");
        var logsUrl = Env("AXIOM_LOGS_URL");
        if (!string.IsNullOrEmpty(logsDs) && !string.IsNullOrEmpty(logsUrl))
        {
            try
            {
                _loggerFactory = LoggerFactory.Create(builder =>
                {
                    // Solo el resumen (categoría dedicada) + Warning/Error del resto llegan a Axiom.
                    builder.AddFilter("Observability.Summary", LogLevel.Information);
                    builder.AddFilter((category, level) =>
                        category == "Observability.Summary" ? level >= LogLevel.Information
                                                               : level >= LogLevel.Warning);
                    builder.AddOpenTelemetry(o =>
                    {
                        o.IncludeFormattedMessage = true;
                        o.SetResourceBuilder(BuildResource());
                        o.AddOtlpExporter(e =>
                        {
                            e.Endpoint = new Uri(logsUrl!);
                            e.Protocol = OpenTelemetry.Exporter.OtlpExportProtocol.HttpProtobuf;
                            e.Headers = $"Authorization=Bearer {token},X-Axiom-Dataset={logsDs}";
                        });
                    });
                });
                SummaryLogger = _loggerFactory.CreateLogger("Observability.Summary");
            }
            catch (Exception e) { Console.Error.WriteLine($"[obs] setup de logs falló: {e}"); }
        }
    }

    public static void Shutdown() { _tracer?.Dispose(); _loggerFactory?.Dispose(); }

    // --- Log estructurado a Axiom: UNO por unidad de trabajo, solo para unidades de BAJA frecuencia
    // (job, corrida de CLI, lote). Emitilo DENTRO del span para que herede el trace_id. ---
    public static void LogSummary(string operation, string component, string outcome, double durationMs,
        IReadOnlyDictionary<string, object>? metrics = null)
    {
        // Los pares se emiten como structured state -> atributos consultables en Axiom.
        using var scope = SummaryLogger.BeginScope(BuildState(operation, component, outcome, durationMs, metrics));
        SummaryLogger.LogInformation("[SUMMARY] {operation} outcome={outcome} duration_ms={ms}", operation, outcome, durationMs);
    }

    private static Dictionary<string, object> BuildState(string operation, string comp, string outcome, double ms,
        IReadOnlyDictionary<string, object>? metrics)
    {
        var s = new Dictionary<string, object>
        {
            ["event.type"] = "operation_summary", ["operation.name"] = operation, ["component"] = comp,
            ["outcome"] = outcome, ["duration_ms"] = Math.Round(ms, 2),
        };
        if (metrics != null) foreach (var kv in metrics) s[kv.Key] = kv.Value;
        return s;
    }

    // --- Instrumentación (no-op si no hay ActivitySource listeners / traces sin configurar) -------
    private static ActivityKind Kind(string k) => k switch
    {
        "consumer" => ActivityKind.Consumer, "server" => ActivityKind.Server,
        "producer" => ActivityKind.Producer, "client" => ActivityKind.Client, _ => ActivityKind.Internal,
    };

    /// <summary>Ejecuta la acción dentro del span de una unidad de trabajo. <paramref name="kind"/>:
    /// "server" (request HTTP entrante), "consumer" (mensaje de cola), "producer" (encolar),
    /// "client" (llamada saliente), "internal" (job, CLI, cómputo). Registra excepción + status
    /// ERROR y la RE-LANZA.</summary>
    public static T WorkSpan<T>(string name, string kind, Func<Activity?, T> fn)
    {
        using var activity = Source.StartActivity(name, Kind(kind));
        try { return fn(activity); }
        catch (Exception ex)
        {
            activity?.SetStatus(ActivityStatusCode.Error, ex.Message);
            activity?.AddTag("exception.type", ex.GetType().FullName);
            activity?.AddTag("exception.message", ex.Message);
            throw;
        }
    }

    /// <summary>Span CONSUMER que continúa el trace propagado en el mensaje (carrier con traceparent).</summary>
    public static T ConsumerSpan<T>(string name, IDictionary<string, string>? carrier, Func<Activity?, T> fn)
    {
        var parentCtx = carrier == null ? default
            : Propagator.Extract(default, carrier, (c, key) => c.TryGetValue(key, out var v) ? new[] { v } : Array.Empty<string>());
        using var activity = Source.StartActivity(name, ActivityKind.Consumer, parentCtx.ActivityContext);
        try { return fn(activity); }
        catch (Exception ex) { activity?.SetStatus(ActivityStatusCode.Error, ex.Message); throw; }
    }

    /// <summary>Inyecta el traceparent del span activo en el mensaje (llamalo antes de encolar).</summary>
    public static IDictionary<string, string> InjectTraceContext(IDictionary<string, string> carrier)
    {
        if (Activity.Current is { } a)
            Propagator.Inject(new PropagationContext(a.Context, Baggage.Current), carrier, (c, k, v) => c[k] = v);
        return carrier;
    }

    private sealed class SelectiveRootSampler : Sampler
    {
        private readonly Sampler _ratio;
        private readonly Sampler _always = new AlwaysOnSampler();
        public SelectiveRootSampler(double rate) => _ratio = new TraceIdRatioBasedSampler(rate);
        public override SamplingResult ShouldSample(in SamplingParameters p)
            => (HighFrequencySpans.Contains(p.Name) ? _ratio : _always).ShouldSample(p);
    }

    private sealed class NullLogger : ILogger
    {
        public IDisposable BeginScope<TState>(TState state) where TState : notnull => NullScope.Instance;
        public bool IsEnabled(LogLevel logLevel) => false;
        public void Log<TState>(LogLevel l, EventId e, TState s, Exception? ex, Func<TState, Exception?, string> f) { }
        private sealed class NullScope : IDisposable { public static readonly NullScope Instance = new(); public void Dispose() { } }
    }
}
