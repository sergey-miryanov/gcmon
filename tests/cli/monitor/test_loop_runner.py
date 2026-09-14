"""Tests for `run_monitoring_loop`."""

from unittest.mock import ANY, MagicMock

import pytest

from gcmon.monitoring.events_reader import RemoteEventsReader
from gcmon.stats.streaming_stats import PauseTotals
from gcmon.stats.views import StatsView


class TestRunMonitoringLoop:
    """Tests for run_monitoring_loop."""

    @pytest.fixture
    def mock_factory(self) -> MagicMock:
        factory = MagicMock()
        runner = MagicMock()
        runner.start.return_value = MagicMock(pid=12345)
        runner.returncode = None
        factory.return_value = runner
        return factory

    @pytest.fixture
    def mock_wait_policy_factory(self) -> MagicMock:
        return MagicMock()

    def test_success(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        result = run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        assert result == 0
        assert "Monitoring complete" in caplog.text
        assert "Total events: 5" in caplog.text

    def test_exception_returns_1(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        mock_loop_runner_deps["RunnerFactory"].side_effect = RuntimeError("test error")

        result = run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())
        assert result == 1
        assert "Failed to run GC monitor" in caplog.text

    def test_loop_run_exception_returns_1(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        mock_loop_runner_deps["MonitorLoop"].return_value.run.side_effect = RuntimeError("runtime error")

        result = run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())
        assert result == 1
        assert "Failed to run GC monitor" in caplog.text

    def test_returns_child_returncode(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        runner = mock_factory.return_value
        runner.returncode = 42

        result = run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())
        assert result == 42

    def test_stdout_format_no_trace_path(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        mock_loop_runner_deps["StreamingStats"].return_value.count.return_value = 0

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options(output_format="stdout"))

        assert "Trace saved to" not in caplog.text

    def test_a_lossy_run_qualifies_the_count(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        """The command logs whatever `summary_lines` builds. What it builds is
        `tests/stats/test_stats_output.py`'s; that it reaches the log is here."""
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        stats = mock_loop_runner_deps["StreamingStats"].return_value
        stats.count.return_value = 1234
        stats.pause_totals_by_gen.return_value = {0: PauseTotals(1234, 0.0, 8566, 0)}

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        assert "Total events: 1234 (+8566 reconstructed, 12.6% observed)" in caplog.text

    def test_the_whole_summary_logs_at_one_level(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        """A count at one level and the figure qualifying it at another is a
        `--log-level` away from a number with nothing to read it against."""
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop
        from gcmon.stats.stats_output import summary_lines

        stats = mock_loop_runner_deps["StreamingStats"].return_value
        stats.count.return_value = 1234
        stats.pause_totals_by_gen.return_value = {0: PauseTotals(1234, 0.0, 8566, 0)}

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        expected = summary_lines(stats, monitoring_options().output_path)
        logged = [record for record in caplog.records if record.getMessage() in expected]
        assert [record.getMessage() for record in logged] == expected
        assert {record.levelname for record in logged} == {"INFO"}

    def test_a_view_calls_print_stats(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        mock_loop_runner_deps["StreamingStats"].return_value.count.return_value = 0

        options = monitoring_options(stats_view=StatsView.TOTAL)
        result = run_monitoring_loop(mock_factory, mock_wait_policy_factory, options)

        assert result == 0
        # The view the operator typed reaches the table.
        assert mock_loop_runner_deps["print_stats"].call_args.args[1] is StatsView.TOTAL

    def test_no_view_prints_no_table(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        mock_loop_runner_deps["print_stats"].assert_not_called()

    def test_a_view_drops_the_pointer_to_stats(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        """Only the command knows the table is coming, so it tells the summary
        not to send the reader looking for it."""
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        stats = mock_loop_runner_deps["StreamingStats"].return_value
        stats.count.return_value = 1234
        stats.pause_totals_by_gen.return_value = {0: PauseTotals(1234, 0.0, 8566, 0)}

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options(stats_view=StatsView.FULL))

        assert "Total events: 1234 (+8566 reconstructed, 12.6% observed)" in caplog.text
        assert "Run with --stats" not in caplog.text

    def test_factory_called_with_control_address(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        mock_factory.assert_called_once_with("/tmp/test-address")

    def test_runner_entered_as_context(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        runner = mock_factory.return_value

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        runner.__enter__.assert_called_once()
        runner.__exit__.assert_called_once()

    def test_control_server_started(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        mock_control_instance = mock_loop_runner_deps["ControlServer"].return_value

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        mock_control_instance.start.assert_called_once()
        mock_control_instance.__enter__.assert_called_once()
        mock_control_instance.__exit__.assert_called_once()

    def test_the_control_server_drains_before_the_exporter_closes(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        """Whatever a client sent last has to reach an exporter still open.

        A pyperf worker lands every mark at teardown, so the control server can
        still be draining when the run ends.
        """
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        order: list[str] = []
        control = mock_loop_runner_deps["ControlServer"].return_value
        control.close.side_effect = lambda: order.append("control server closed")
        monitor = mock_loop_runner_deps["EventsMonitor"].return_value
        monitor.__exit__.side_effect = lambda *args: order.append("monitor stopped")

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        assert order[:2] == ["control server closed", "monitor stopped"], (
            f"the exporter closes before the control plane has drained: {order}"
        )

    def test_enabled_uses_control_server(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        mock_control_instance = mock_loop_runner_deps["ControlServer"].return_value

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        # The predicate reaches the monitor now, not the loop: it decides
        # whether a pid is polled at all, which is a per-pid lifetime question.
        assert mock_loop_runner_deps["EventsMonitor"].call_args.kwargs["is_pid_enabled"] == (
            mock_control_instance.is_enabled
        )
        mock_loop_runner_deps["MonitorLoop"].assert_called_once_with(
            ANY,
            ANY,
            rate=0.1,
            rss_sampler=None,
        )

    def test_monitor_constructed_from_process_exporter_and_stats(
        self,
        mock_factory: MagicMock,
        mock_wait_policy_factory: MagicMock,
        monitoring_options: MagicMock,
        mock_loop_runner_deps: dict[str, MagicMock],
    ) -> None:
        """Pins what the fixture patches: every test above relies on the monitor
        being a mock, and a patch aimed at a name the command path no longer
        imports would leave a real one in its place without failing anything."""
        from gcmon.cli.monitor.loop_runner import run_monitoring_loop

        monitor_cls = mock_loop_runner_deps["EventsMonitor"]
        process = mock_factory.return_value.start.return_value
        exporter = mock_loop_runner_deps["EventsExporterFactory"].return_value.return_value
        stats = mock_loop_runner_deps["StreamingStats"].return_value

        run_monitoring_loop(mock_factory, mock_wait_policy_factory, monitoring_options())

        mock_control_instance = mock_loop_runner_deps["ControlServer"].return_value
        monitor_cls.assert_called_once_with(
            process,
            exporter,
            stats,
            reader=ANY,
            wait_policy_factory=mock_wait_policy_factory,
            is_pid_enabled=mock_control_instance.is_enabled,
            registry=ANY,
        )
        # The one place in the package that builds a real reader. Named rather
        # than matched loosely: a monitor built with a fake here would read
        # nothing in production and no other test would notice.
        assert isinstance(monitor_cls.call_args.kwargs["reader"], RemoteEventsReader)
        assert mock_loop_runner_deps["MonitorLoop"].call_args.args[0] is monitor_cls.return_value
