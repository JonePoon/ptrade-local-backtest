"""With prepared local data, strategy backtests must not require a cloud connection."""

from __future__ import annotations

import socket

import pytest

pytestmark = pytest.mark.integration

STRATEGY = """
def initialize(context):
    set_universe(['000001.SZ'])
    g.seen = 0
    run_daily(context, buy, time='09:31')

def buy(context):
    order_target('000001.SZ', 100)

def handle_data(context, data):
    g.seen += len(data)
"""


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_prepared_local_backtest_succeeds_when_network_is_unavailable(
    engine_factory, monkeypatch, backend
):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append(args)
        raise OSError("controlled offline test: network unavailable")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    engine = engine_factory(STRATEGY, acceleration={"backend": backend})
    daily = engine.run()
    trades = engine.trades_frame()
    assert daily.height == 5
    assert trades.height > 0 and engine.g.seen > 0
    assert engine.feed.src.query_error_count == 0 and not engine._failed_funcs
    assert attempts == [], "Local data backtests must not attempt a PTrade/cloud connection"
    assert engine.feed.acceleration["effective"] == backend
