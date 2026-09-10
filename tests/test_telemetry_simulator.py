import random
from unittest.mock import Mock, patch

import telemetry_simulator as sim


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

    with patch("telemetry_simulator.requests.post", return_value=mock_response) as mock_post:
        sim.post_metrics("http://localhost:8428/write", "some,line=protocol value=1 1")

    mock_post.assert_called_once_with(
        "http://localhost:8428/write", data="some,line=protocol value=1 1"
    )
