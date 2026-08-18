from aef_grits.network_health import NetworkHealthMonitor


def test_network_monitor_reports_and_deduplicates_counter_warnings():
    samples = iter(
        [
            {"enp99s0": {"rx_dropped": 10, "rx_missed_errors": 20}},
            {"enp99s0": {"rx_dropped": 12, "rx_missed_errors": 25}},
            {"enp99s0": {"rx_dropped": 12, "rx_missed_errors": 25}},
            {"enp99s0": {"rx_dropped": 13, "rx_missed_errors": 25}},
        ]
    )
    monitor = NetworkHealthMonitor(reader=lambda: next(samples))

    first = monitor.consume_warning()
    assert first["interfaces"]["enp99s0"] == {
        "rx_dropped": 2,
        "rx_missed_errors": 5,
    }
    assert monitor.consume_warning() is None
    second = monitor.consume_warning()
    assert second["interfaces"]["enp99s0"] == {"rx_dropped": 3}
