"""Paper fills, cash accounting and the NSE cost model."""

from __future__ import annotations

import pytest

from nifty_options.brokers.base import OrderRequest, OrderStatus, OrderType, Side, option_charges
from nifty_options.brokers.paper import PaperBroker


@pytest.fixture
def broker(config, fake_client) -> PaperBroker:
    return PaperBroker(config, fake_client)


def buy(instrument="NSE_FO|C24000", qty=75, price=100.0) -> OrderRequest:
    return OrderRequest(
        instrument_key=instrument, symbol="NIFTY 24000 CE", side=Side.BUY,
        quantity=qty, price=price, strategy="track_a_intraday_debit",
    )


def sell(instrument="NSE_FO|C24000", qty=75, price=100.0) -> OrderRequest:
    return OrderRequest(
        instrument_key=instrument, symbol="NIFTY 24000 CE", side=Side.SELL,
        quantity=qty, price=price, strategy="track_a_intraday_debit",
    )


def test_fills_are_immediate(broker):
    result = broker.place_order(buy())
    assert result.status is OrderStatus.COMPLETE
    assert result.filled_quantity == 75


def test_buy_pays_up_and_sell_receives_less(broker):
    bought = broker.place_order(buy(price=100.0))
    sold = broker.place_order(sell(price=100.0))
    assert bought.average_price > 100.0          # slippage against the buyer
    assert sold.average_price < 100.0            # and against the seller


def test_slippage_respects_the_tick_size(broker):
    price = broker.place_order(buy(price=100.0)).average_price
    assert round(price / 0.05) * 0.05 == pytest.approx(price, abs=1e-9)


def test_limit_orders_fill_at_the_limit(broker):
    request = buy(price=95.0)
    request.order_type = OrderType.LIMIT
    assert broker.place_order(request).average_price == 95.0


def test_charges_are_deducted_from_cash(broker):
    starting = broker.available_margin()
    result = broker.place_order(buy(price=100.0))
    expected = starting - result.average_price * 75 - result.charges
    assert broker.available_margin() == pytest.approx(expected, rel=1e-9)


def test_round_trip_books_realised_pnl(broker):
    broker.place_order(buy(price=100.0))
    broker.place_order(sell(price=140.0))
    assert broker.realized_pnl > 0
    assert broker.get_positions() == []


def test_losing_round_trip_is_negative(broker):
    broker.place_order(buy(price=100.0))
    broker.place_order(sell(price=80.0))
    assert broker.realized_pnl < 0


def test_short_then_cover_books_pnl(broker):
    broker.place_order(sell(price=100.0))
    broker.place_order(buy(price=60.0))
    assert broker.realized_pnl > 0                # sold high, covered low


def test_position_is_tracked_while_open(broker):
    broker.place_order(buy(price=100.0))
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == 75


def test_averaging_up_blends_the_cost(broker):
    broker.place_order(buy(price=100.0))
    broker.place_order(buy(price=120.0))
    position = broker.get_positions()[0]
    assert position.quantity == 150
    assert 100 < position.average_price < 121


def test_state_survives_a_restart(broker, config, fake_client):
    broker.place_order(buy(price=100.0))
    reopened = PaperBroker(config, fake_client)
    assert len(reopened.get_positions()) == 1
    assert reopened.available_margin() == pytest.approx(broker.available_margin())


def test_reset_restores_starting_capital(broker, config):
    broker.place_order(buy(price=100.0))
    broker.reset()
    assert broker.available_margin() == config.track_a.capital + config.track_b.capital
    assert broker.get_positions() == []


def test_square_off_all_flattens(broker):
    broker.place_order(buy(price=100.0))
    broker.place_order(sell(instrument="NSE_FO|P24000", price=90.0))
    broker.square_off_all()
    assert broker.get_positions() == []


def test_rejects_when_no_price_is_available(config):
    broker = PaperBroker(config, client=None)
    result = broker.place_order(buy(price=0.0))
    assert result.status is OrderStatus.REJECTED


# ---------------------------------------------------------------------- #
# cost model
# ---------------------------------------------------------------------- #
def test_sell_side_carries_stt():
    turnover = 100.0 * 75
    assert option_charges(turnover, Side.SELL) > option_charges(turnover, Side.BUY)


def test_charges_scale_with_turnover():
    assert option_charges(200 * 75, Side.BUY) > option_charges(100 * 75, Side.BUY)


def test_charges_can_be_reduced_to_brokerage_only():
    assert option_charges(100 * 75, Side.BUY, 20.0, apply_statutory=False) == 20.0


def test_a_round_trip_costs_roughly_a_discount_broker_rate():
    """Sanity band: one Nifty option round trip should cost well under Rs 150."""
    cost = option_charges(120 * 75, Side.BUY) + option_charges(150 * 75, Side.SELL)
    assert 40 < cost < 150
