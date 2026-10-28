import random
from unittest.mock import Mock, patch

import ingestion.telemetry_simulator as sim


def test_format_line_protocol_builds_expected_string():
    line = sim.format_line_protocol(
        device="Router-A",
        interface="GigabitEthernet0/0/0",
        metrics={"in_octets": 12345, "out_octets": 6789, "input_drops": 0},
        timestamp_ns=1700000000000000000,
    )
    assert line == (
        "interface_metrics,device=Router-A,interface=GigabitEthernet0/0/0 "
        "in_octets=12345,out_octets=6789,input_drops=0 1700000000000000000"
    )


def test_generate_metrics_returns_nonnegative_integers():
    rng = random.Random(42)
    metrics = sim.generate_metrics(rng)

    assert set(metrics.keys()) == {"in_octets", "out_octets", "input_drops"}
    for value in metrics.values():
        assert isinstance(value, int)
        assert value >= 0


def test_build_payload_includes_both_routers():
    rng = random.Random(1)
    payload = sim.build_payload(rng)

    assert "device=Router-A,interface=GigabitEthernet0/0/0" in payload
    assert "device=Router-B,interface=GigabitEthernet0/0/1" in payload
    assert payload.count("\n") == 1  # two lines, one newline separator


def test_generate_metrics_defaults_to_no_drops():
    for seed in range(200):
        rng = random.Random(seed)
        metrics = sim.generate_metrics(rng)
        assert metrics["input_drops"] == 0

    # Explicit False behaves the same as the default.
    for seed in range(200):
        rng = random.Random(seed)
        metrics = sim.generate_metrics(rng, inject_drops=False)
        assert metrics["input_drops"] == 0


def test_generate_metrics_can_inject_drops_when_requested():
    saw_zero = False
    saw_nonzero = False
    for seed in range(200):
        rng = random.Random(seed)
        input_drops = sim.generate_metrics(rng, inject_drops=True)["input_drops"]
        if input_drops == 0:
            saw_zero = True
        else:
            saw_nonzero = True

    assert saw_zero
    assert saw_nonzero


def test_build_payload_propagates_inject_drops_flag():
    """A typo dropping inject_drops inside build_payload's call to
    generate_metrics would not be caught by the generate_metrics-only tests
    above, so exercise the propagation through build_payload directly."""
    saw_nonzero_when_enabled = False
    for seed in range(200):
        rng = random.Random(seed)
        payload = sim.build_payload(rng, inject_drops=True)
        for line in payload.splitlines():
            fields = line.split(" ")[1]
            drops = int(fields.split("input_drops=")[1].split(",")[0])
            if drops != 0:
                saw_nonzero_when_enabled = True

    assert saw_nonzero_when_enabled

    for seed in range(200):
        rng = random.Random(seed)
        payload = sim.build_payload(rng, inject_drops=False)
        for line in payload.splitlines():
            fields = line.split(" ")[1]
            drops = int(fields.split("input_drops=")[1].split(",")[0])
            assert drops == 0

        # Default (no explicit inject_drops) must behave the same as False.
        rng_default = random.Random(seed)
        payload_default = sim.build_payload(rng_default)
        for line in payload_default.splitlines():
            fields = line.split(" ")[1]
            drops = int(fields.split("input_drops=")[1].split(",")[0])
            assert drops == 0


def test_post_metrics_sends_payload_to_write_endpoint():
    mock_response = Mock()
    mock_response.raise_for_status = Mock()

    with patch("ingestion.telemetry_simulator.requests.post", return_value=mock_response) as mock_post:
        sim.post_metrics("http://localhost:8428/write", "some,line=protocol value=1 1")

    mock_post.assert_called_once_with(
        "http://localhost:8428/write", data="some,line=protocol value=1 1"
    )


HSI_FIELDS = {
    "latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "downstream_bandwidth_mbps",
    "upstream_bandwidth_mbps",
}


def test_hsi_fields_present_only_with_flag():
    for seed in range(50):
        plain = sim.generate_metrics(random.Random(seed))
        assert not HSI_FIELDS & set(plain)
        hsi = sim.generate_metrics(random.Random(seed), hsi=True)
        assert HSI_FIELDS <= set(hsi)
        # The interface counters are still there alongside the HSI fields.
        assert {"in_octets", "out_octets", "input_drops"} <= set(hsi)


def test_hsi_values_are_finite_floats():
    import math

    for seed in range(200):
        metrics = sim.generate_metrics(random.Random(seed), hsi=True)
        for field in HSI_FIELDS:
            value = metrics[field]
            assert isinstance(value, float), field
            assert math.isfinite(value), field
            assert value >= 0, field


def test_hsi_compliant_values_satisfy_the_pinned_demo_bounds():
    for seed in range(200):
        m = sim.generate_metrics(random.Random(seed), hsi=True)
        assert m["downstream_bandwidth_mbps"] >= sim.HSI_DEMO_BOUNDS["downstream_bandwidth_mbps"]
        assert m["upstream_bandwidth_mbps"] >= sim.HSI_DEMO_BOUNDS["upstream_bandwidth_mbps"]
        assert m["latency_ms"] < sim.HSI_DEMO_BOUNDS["latency_ms"]
        assert m["jitter_ms"] < sim.HSI_DEMO_BOUNDS["jitter_ms"]
        assert m["packet_loss_percent"] < sim.HSI_DEMO_BOUNDS["packet_loss_percent"]


def test_breach_flag_violates_the_pinned_latency_bound():
    # 25 ms is the latency bound of the public HSI demo intent; pinned here so a
    # change to the simulator's constant cannot silently move the test.
    assert sim.HSI_DEMO_BOUNDS["latency_ms"] == 25.0
    for seed in range(200):
        m = sim.generate_metrics(random.Random(seed), hsi=True, hsi_breach=True)
        assert m["latency_ms"] > 25.0
        # Only latency breaches: the other four stay compliant.
        assert m["jitter_ms"] < sim.HSI_DEMO_BOUNDS["jitter_ms"]
        assert m["packet_loss_percent"] < sim.HSI_DEMO_BOUNDS["packet_loss_percent"]
        assert m["downstream_bandwidth_mbps"] >= sim.HSI_DEMO_BOUNDS["downstream_bandwidth_mbps"]
        assert m["upstream_bandwidth_mbps"] >= sim.HSI_DEMO_BOUNDS["upstream_bandwidth_mbps"]


def test_build_payload_propagates_hsi_flags():
    payload = sim.build_payload(random.Random(3), hsi=True, hsi_breach=True)
    for line in payload.splitlines():
        fields = dict(kv.split("=") for kv in line.split(" ")[1].split(","))
        assert HSI_FIELDS <= set(fields)
        assert float(fields["latency_ms"]) > 25.0
    plain = sim.build_payload(random.Random(3))
    assert "latency_ms" not in plain


def test_hsi_float_fields_render_as_line_protocol_floats():
    line = sim.format_line_protocol("Router-A", "Gi0", {"latency_ms": 12.0, "input_drops": 0}, 1)
    # A float written as "12.0" stays a float field; an int stays an int.
    assert "latency_ms=12.0" in line
    assert "input_drops=0" in line
