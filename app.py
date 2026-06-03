"""
Sample Python application instrumented with OpenTelemetry.
Emits metrics (counter, histogram, gauge), traces, and structured logs
to the CloudWatch agent via OTLP.
"""

import time
import random
import logging
from flask import Flask, jsonify, request

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor

# ---- Resource definition ----
resource = Resource.create({
    SERVICE_NAME: "otel-demo-app",
    SERVICE_VERSION: "1.0.0",
    "deployment.environment": "dev",
})

# ---- Traces setup ----
trace_exporter = OTLPSpanExporter()  # Uses OTEL_EXPORTER_OTLP_ENDPOINT env var
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanExporter(trace_exporter))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(__name__)

# ---- Metrics setup ----
metric_exporter = OTLPMetricExporter()  # Uses OTEL_EXPORTER_OTLP_ENDPOINT env var
metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=10000)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Define metrics instruments
request_counter = meter.create_counter(
    name="http_requests_total",
    description="Total number of HTTP requests",
    unit="1",
)

latency_histogram = meter.create_histogram(
    name="http_request_duration_ms",
    description="HTTP request latency in milliseconds",
    unit="ms",
)

active_requests_gauge = meter.create_up_down_counter(
    name="http_active_requests",
    description="Number of active HTTP requests",
    unit="1",
)

# ---- Logging setup ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---- Flask app ----
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


@app.route("/")
def index():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "service": "otel-demo-app"})


@app.route("/api/orders", methods=["GET"])
def get_orders():
    """Simulates fetching orders with realistic latency and tracing."""
    active_requests_gauge.add(1, {"endpoint": "/api/orders", "method": "GET"})

    with tracer.start_as_current_span("get_orders") as span:
        # Simulate database query
        with tracer.start_as_current_span("db_query"):
            latency = random.uniform(5, 50)
            time.sleep(latency / 1000)
            span.set_attribute("db.system", "postgresql")
            span.set_attribute("db.statement", "SELECT * FROM orders LIMIT 10")

        # Simulate some processing
        with tracer.start_as_current_span("process_results"):
            time.sleep(random.uniform(1, 10) / 1000)
            order_count = random.randint(1, 10)
            span.set_attribute("orders.count", order_count)

        # Record metrics
        total_latency = latency + random.uniform(1, 10)
        request_counter.add(1, {"endpoint": "/api/orders", "method": "GET", "status": "200"})
        latency_histogram.record(total_latency, {"endpoint": "/api/orders", "method": "GET"})

        logger.info(f"Fetched {order_count} orders in {total_latency:.1f}ms")

    active_requests_gauge.add(-1, {"endpoint": "/api/orders", "method": "GET"})

    orders = [{"id": i, "item": f"item-{i}", "price": round(random.uniform(10, 100), 2)} for i in range(order_count)]
    return jsonify({"orders": orders, "count": order_count})


@app.route("/api/orders", methods=["POST"])
def create_order():
    """Simulates creating an order."""
    active_requests_gauge.add(1, {"endpoint": "/api/orders", "method": "POST"})

    with tracer.start_as_current_span("create_order") as span:
        # Simulate validation
        with tracer.start_as_current_span("validate_order"):
            time.sleep(random.uniform(2, 8) / 1000)

        # Simulate DB write
        with tracer.start_as_current_span("db_insert"):
            latency = random.uniform(10, 80)
            time.sleep(latency / 1000)
            span.set_attribute("db.system", "postgresql")
            span.set_attribute("db.statement", "INSERT INTO orders ...")

        # Simulate occasional errors (10% chance)
        if random.random() < 0.1:
            span.set_attribute("error", True)
            request_counter.add(1, {"endpoint": "/api/orders", "method": "POST", "status": "500"})
            latency_histogram.record(latency, {"endpoint": "/api/orders", "method": "POST"})
            logger.error("Failed to create order - database timeout")
            active_requests_gauge.add(-1, {"endpoint": "/api/orders", "method": "POST"})
            return jsonify({"error": "Internal server error"}), 500

        request_counter.add(1, {"endpoint": "/api/orders", "method": "POST", "status": "201"})
        latency_histogram.record(latency, {"endpoint": "/api/orders", "method": "POST"})
        logger.info(f"Order created in {latency:.1f}ms")

    active_requests_gauge.add(-1, {"endpoint": "/api/orders", "method": "POST"})

    return jsonify({"id": random.randint(1000, 9999), "status": "created"}), 201


@app.route("/api/slow")
def slow_endpoint():
    """Deliberately slow endpoint to generate interesting latency data."""
    with tracer.start_as_current_span("slow_operation") as span:
        delay = random.uniform(200, 2000)
        span.set_attribute("delay_ms", delay)
        time.sleep(delay / 1000)

        request_counter.add(1, {"endpoint": "/api/slow", "method": "GET", "status": "200"})
        latency_histogram.record(delay, {"endpoint": "/api/slow", "method": "GET"})

    return jsonify({"message": "done", "delay_ms": round(delay, 1)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
