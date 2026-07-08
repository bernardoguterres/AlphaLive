"""
Tests for alphalive.main.main() - the orchestration entry point.

Everything I/O-bound (broker, market data, Telegram, DeepLOB/AlphaSignal
clients, state file) is mocked out via patching the names imported into
alphalive.main. The infinite `while True` loop is exited deterministically
by making time.sleep() raise KeyboardInterrupt after a bounded number of
calls - main() treats KeyboardInterrupt as a clean shutdown signal, so this
lets each test exercise exactly one loop iteration (or a chosen error path)
without hanging or touching any real network/broker.
"""

from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from alphalive import main as main_module
from alphalive.config import AppConfig, BrokerConfig, TelegramConfig

ET = ZoneInfo("America/New_York")


def _make_app_config(**overrides):
    cfg = AppConfig(
        broker=BrokerConfig(api_key="test_key", secret_key="test_secret", paper=True),
        telegram=TelegramConfig(enabled=False),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class _StopLoop(KeyboardInterrupt):
    """Distinct subclass so we can tell "test ended the loop on purpose" apart
    from a real bug that raises KeyboardInterrupt for some other reason."""


def _sleep_raises_after(n_calls):
    """Return a side_effect list: allow n_calls sleeps, then stop the loop."""
    calls = {"n": 0}

    def _side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= n_calls:
            raise _StopLoop()

    return _side_effect


@pytest.fixture
def main_mocks(sample_strategy_config):
    """Patch every external dependency main() touches."""
    with patch("alphalive.main.load_config_path") as m_load_cfg, \
         patch("alphalive.main.load_env") as m_load_env, \
         patch("alphalive.main.validate_all") as m_validate, \
         patch("alphalive.main.BotState") as m_state_cls, \
         patch("alphalive.main.AlpacaBroker") as m_broker_cls, \
         patch("alphalive.main.MarketDataFetcher") as m_md_cls, \
         patch("alphalive.main.SignalEngine") as m_signal_cls, \
         patch("alphalive.main.RiskManager") as m_risk_cls, \
         patch("alphalive.main.OrderManager") as m_om_cls, \
         patch("alphalive.main.TelegramNotifier") as m_notifier_cls, \
         patch("alphalive.main.TelegramCommandListener") as m_cmd_cls, \
         patch("alphalive.main.signal.signal"), \
         patch("alphalive.main.time.sleep") as m_sleep:

        m_load_cfg.return_value = [sample_strategy_config]
        m_load_env.return_value = _make_app_config()
        m_validate.return_value = True

        m_state = m_state_cls.return_value
        m_state.check_dashboard_paused.return_value = False

        m_broker = m_broker_cls.return_value
        m_broker.connect.return_value = True
        m_broker.is_market_open.return_value = False  # default: closed, weekend path
        m_broker.get_account.return_value = Mock(equity=100000.0)
        m_broker.get_all_positions.return_value = []

        m_md = m_md_cls.return_value
        import pandas as pd
        m_md.get_latest_bars.return_value = pd.DataFrame({
            "open": [100.0] * 5, "high": [101.0] * 5, "low": [99.0] * 5,
            "close": [100.5] * 5, "volume": [1000] * 5,
        })

        m_signal_engine = m_signal_cls.return_value
        m_signal_engine.generate_signal.return_value = {
            "warmup_complete": True, "signal": "HOLD", "confidence": 0.5, "reason": "n/a",
        }

        m_notifier = m_notifier_cls.return_value

        m_cmd_listener = m_cmd_cls.return_value
        m_cmd_listener.thread = Mock(is_alive=Mock(return_value=True))

        yield {
            "load_config_path": m_load_cfg,
            "load_env": m_load_env,
            "validate_all": m_validate,
            "BotState": m_state_cls,
            "state": m_state,
            "AlpacaBroker": m_broker_cls,
            "broker": m_broker,
            "MarketDataFetcher": m_md_cls,
            "market_data": m_md,
            "SignalEngine": m_signal_cls,
            "signal_engine": m_signal_engine,
            "RiskManager": m_risk_cls,
            "OrderManager": m_om_cls,
            "TelegramNotifier": m_notifier_cls,
            "notifier": m_notifier,
            "TelegramCommandListener": m_cmd_cls,
            "cmd_listener": m_cmd_listener,
            "sleep": m_sleep,
        }


# ---------------------------------------------------------------------------
# Startup failure paths
# ---------------------------------------------------------------------------


def test_main_exits_when_validation_fails(main_mocks):
    main_mocks["validate_all"].return_value = False

    with pytest.raises(SystemExit) as exc_info:
        main_module.main(config_path="dummy.json")

    assert exc_info.value.code == 1
    main_mocks["AlpacaBroker"].assert_not_called()


def test_main_exits_when_broker_connect_fails(main_mocks):
    main_mocks["broker"].connect.return_value = False

    with pytest.raises(SystemExit) as exc_info:
        main_module.main(config_path="dummy.json")

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Successful startup + weekend / pre-market / after-hours loop bodies
# ---------------------------------------------------------------------------


def test_main_weekend_sleep_path(main_mocks):
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    # Saturday
    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 6, 10, 0, tzinfo=ET)  # Sat
        main_module.main(config_path="dummy.json")  # returns after KeyboardInterrupt break

    main_mocks["notifier"].send_message.assert_called()  # startup message sent


def test_main_dashboard_paused_sleeps(main_mocks):
    main_mocks["state"].check_dashboard_paused.return_value = True
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    main_module.main(config_path="dummy.json")

    main_mocks["sleep"].assert_called_with(30)


def test_main_market_open_runs_signal_and_exit_checks(main_mocks):
    main_mocks["broker"].is_market_open.return_value = True
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 2, 9, 40, tzinfo=ET)  # Tue, market open
        main_module.main(config_path="dummy.json")

    # Exit checks and signal checks should have touched the broker
    assert main_mocks["broker"].get_all_positions.called
    assert main_mocks["broker"].get_account.called


def test_main_cmd_listener_thread_died_alerts(main_mocks):
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["cmd_listener"].thread.is_alive.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    # Enable telegram so cmd_listener is created
    main_mocks["load_env"].return_value = _make_app_config(
        telegram=TelegramConfig(bot_token="tok", chat_id="123")
    )

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 6, 10, 0, tzinfo=ET)
        main_module.main(config_path="dummy.json")

    alerts = [c.args[0] for c in main_mocks["notifier"].send_error_alert.call_args_list]
    assert any("Command listener offline" in a for a in alerts)


def test_main_generic_loop_exception_is_caught_and_continues(main_mocks):
    # Force an exception inside the loop body (is_market_open raises)
    main_mocks["broker"].is_market_open.side_effect = RuntimeError("broker exploded")
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    # The generic `except Exception` handler's own time.sleep(60) call is not
    # itself guarded against KeyboardInterrupt, so our _StopLoop sentinel
    # propagates out of main() here rather than being caught by the loop's
    # `except KeyboardInterrupt: break`. That's a real (minor) quirk of the
    # error-handling path, not a test bug - assert on it explicitly.
    with pytest.raises(_StopLoop):
        main_module.main(config_path="dummy.json")

    main_mocks["sleep"].assert_called_with(60)  # error path sleeps 60s
    main_mocks["notifier"].send_error_alert.assert_called()


def test_main_dry_run_and_paper_flags_override_config(main_mocks):
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    main_module.main(config_path="dummy.json", dry_run=True, paper=False)

    app_config = main_mocks["load_env"].return_value
    assert app_config.dry_run is True
    assert app_config.broker.paper is False


def test_main_premarket_sleep_path(main_mocks):
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 2, 8, 0, tzinfo=ET)  # Tue, pre-market
        main_module.main(config_path="dummy.json")

    main_mocks["sleep"].assert_called_with(300)


def test_main_after_hours_sends_eod_summary_then_sleeps(main_mocks):
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 2, 16, 30, tzinfo=ET)  # Tue, after hours
        main_module.main(config_path="dummy.json")

    main_mocks["notifier"].send_daily_summary.assert_called_once()
    main_mocks["sleep"].assert_called_with(1800)


def test_main_holiday_closure_sleeps_5_minutes(main_mocks):
    """Weekday, within normal trading hours, but broker reports market closed
    (e.g. a market holiday) - falls through to the generic 5-minute sleep."""
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 2, 12, 0, tzinfo=ET)  # Tue midday
        main_module.main(config_path="dummy.json")

    main_mocks["sleep"].assert_called_with(300)


def test_main_eod_summary_window_at_355pm(main_mocks):
    main_mocks["broker"].is_market_open.return_value = True
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 2, 15, 57, tzinfo=ET)
        main_module.main(config_path="dummy.json")

    main_mocks["notifier"].send_daily_summary.assert_called_once()


def test_main_multi_strategy_setup(main_mocks, sample_strategy_config):
    cfg2 = sample_strategy_config.model_copy(deep=True)
    cfg2.ticker = "MSFT"
    main_mocks["load_config_path"].return_value = [sample_strategy_config, cfg2]
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 6, 10, 0, tzinfo=ET)
        main_module.main(config_path="dummy_dir")

    # SignalEngine/OrderManager/RiskManager constructed once per strategy
    assert main_mocks["SignalEngine"].call_count == 2
    assert main_mocks["OrderManager"].call_count == 2


def test_main_deeplob_and_alphasignal_enabled(main_mocks):
    cfg = _make_app_config()
    cfg.alphasignal.enabled = True
    cfg.deeplob.enabled = True
    main_mocks["load_env"].return_value = cfg
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    with patch("alphalive.main.DeepLOBClient") as m_deeplob_cls, \
         patch("alphalive.main.AlphaSignalClient") as m_alphasignal_cls, \
         patch("alphalive.main.datetime") as m_dt:
        m_dt.now.return_value = datetime(2024, 1, 6, 10, 0, tzinfo=ET)
        main_module.main(config_path="dummy.json")

        m_deeplob_cls.assert_called_once()
        m_alphasignal_cls.assert_called_once()


def test_main_sigterm_handler_sends_shutdown_notification(main_mocks):
    """The SIGTERM handler is registered via signal.signal(); capture and
    invoke it directly to verify it aggregates stats and notifies cleanly."""
    main_mocks["broker"].is_market_open.return_value = False
    main_mocks["sleep"].side_effect = _sleep_raises_after(1)

    captured_handler = {}

    def _capture_signal(sig, handler):
        captured_handler[sig] = handler

    with patch("alphalive.main.signal.signal", side_effect=_capture_signal):
        with patch("alphalive.main.datetime") as m_dt:
            m_dt.now.return_value = datetime(2024, 1, 6, 10, 0, tzinfo=ET)
            main_module.main(config_path="dummy.json")

    import signal as signal_mod
    handler = captured_handler[signal_mod.SIGTERM]

    with pytest.raises(SystemExit) as exc_info:
        handler(signal_mod.SIGTERM, None)

    assert exc_info.value.code == 0
    main_mocks["notifier"].send_shutdown_notification.assert_called_once()


def test_main_replay_mode_runs_simulator_and_exits(main_mocks):
    with patch("alphalive.replay.ReplaySimulator") as m_sim_cls:
        m_sim = m_sim_cls.return_value

        with pytest.raises(SystemExit) as exc_info:
            main_module.main(config_path="dummy.json", replay_mode=True)

        assert exc_info.value.code == 0
        m_sim.run.assert_called_once()

    # Live loop should never have started
    main_mocks["sleep"].assert_not_called()
