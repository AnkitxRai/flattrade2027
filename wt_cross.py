"""Self-contained WaveTrend strategy: candles, ITM options, orders and state.

Call run(api, args) from main.py; this file never logs in.
NIFTY/CRUDE flags, their start/end times, timeframe and indicator/SL settings
are reread from this file every minute. Both markets can run together.
WT1 uses completed candles only, evaluated once per candle; polling remains every minute.
LONG buys the closest strictly ITM call; SHORT buys the closest ITM put.
NIFTY rolls at <=2 inclusive weekdays, matching rmi.py. CRUDE rolls to
next month with <3 calendar days left; candles use that month's futures.
Default: 1 lot; NIFTY/CRUDE option-premium SL distances are separately configurable. NIFTY exit 15:13, CRUDE exit 23:00 IST.
CRUDE uses mini CRUDEOILM options and futures. Missing discovery data blocks
entries; no expiry dates, contract tokens or lot sizes are hardcoded.
Live option stops use broker-held SL-LMT orders after confirmed entry fills. Unknown order
outcomes pause the affected strategy for reconciliation. Runtime JSON/lock files preserve state.
Formula adapted from WaveTrend with Crosses [LazyBear].
"""
import ast
from collections import deque
from datetime import datetime, timedelta, time as dtime
import fcntl
import json
import math
from decimal import Decimal, ROUND_FLOOR
import os
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

# Editable settings: read from this file again before every minute's poll.
NIFTY = True
NIFTY_START_TIME = dtime(9, 45)
NIFTY_END_TIME = dtime(15, 13)
NIFTY_SL_POINTS = 35.0  # option premium points; 0 disables SL

CRUDE = True
CRUDE_START_TIME = dtime(9, 30)
CRUDE_END_TIME = dtime(23, 0)
CRUDE_SL_POINTS = 50.0  # option premium points; 0 disables SL

SL_LIMIT_BUFFER = 5.0  # Sell limit sits this many premium points below the trigger.

NIFTY_TIMEFRAME_MINUTES = 5
CRUDE_TIMEFRAME_MINUTES = 15  # Allowed: 1, 3, 5, 10, 15, 30, 60, 120, 240
# All times are IST. Polling remains every minute. Flags control new entries;
# existing positions still receive SL, reverse-signal and end-time exits.

CHANNEL_LENGTH = 10
AVERAGE_LENGTH = 21
SIGNAL_LENGTH = 4


class WaveTrend:
    def __init__(self):
        self.esa = self.dev = self.tci = None
        self.values = deque(maxlen=SIGNAL_LENGTH)

    def update(self, candle):
        ap = (candle['high'] + candle['low'] + candle['close']) / 3
        alpha = 2 / (CHANNEL_LENGTH + 1)
        self.esa = ap if self.esa is None else self.esa + alpha * (ap-self.esa)
        d = abs(ap-self.esa)
        self.dev = d if self.dev is None else self.dev + alpha * (d-self.dev)
        if self.dev <= 0:
            return None
        ci = (ap-self.esa) / (.015*self.dev)
        self.tci = ci if self.tci is None else self.tci + 2/(AVERAGE_LENGTH+1)*(ci-self.tci)
        return self.tci


class Strategy:
    name = 'wt_cross'

    def observe(self, candles, previous=None):
        if len(candles) < 100:
            raise ValueError('Need at least 100 candles to warm up WaveTrend')
        indicator = WaveTrend()
        value = None
        prior_bar = None
        for candle in candles:
            prior_bar = value
            value = indicator.update(candle)
        # On first startup compare against the preceding completed candle.
        reference = prior_bar if previous is None else previous
        signal = None
        if value is not None and reference is not None:
            if reference <= 0 < value:
                signal = 'LONG'
            elif reference >= 0 > value:
                signal = 'SHORT'
        return {'value': value, 'signal': signal}


def weekdays_inclusive(start, end):
    count = 0
    while start <= end:
        count += start.weekday() < 5
        start += timedelta(days=1)
    return count


def choose_contract(rows, side, spot, today, market="NIFTY", expiry_required=None):
    if side not in ('LONG', 'SHORT') or not math.isfinite(spot) or spot <= 0:
        raise ValueError('Invalid option selection inputs')
    kind = 'C' if side == 'LONG' else 'P'
    strike = (math.ceil(spot/50)-1)*50 if kind == 'C' else (math.floor(spot/50)+1)*50
    root = 'CRUDEOILM' if market == 'CRUDE' else 'NIFTY'
    contracts = {}
    for row in rows:
        symbol = row.get('tsym', '')
        match = re.fullmatch(rf'{root}(\d{{2}}[A-Z]{{3}}\d{{2}})([CP])(\d+(?:\.\d+)?)', symbol)
        if not match or match[2] != kind or float(match[3]) != strike:
            continue
        expiry = datetime.strptime(match[1], '%d%b%y').date()
        if expiry >= today:
            contracts[expiry] = row
    dates = sorted(contracts)
    if not dates:
        raise ValueError('Broker search returned no matching ITM contracts')
    if expiry_required is not None:
        if expiry_required not in contracts:
            raise ValueError('No ITM option for the selected underlying contract month')
        return contracts[expiry_required], expiry_required, strike
    if market == 'CRUDE':
        chosen = crude_expiry(dates, today)
        return contracts[chosen], chosen, strike
    index = 1 if weekdays_inclusive(today, dates[0]) <= 2 else 0
    if index >= len(dates):
        raise ValueError('Next expiry required but not returned by broker; no entry')
    return contracts[dates[index]], dates[index], strike



def crude_expiry(dates, today):
    """Use current monthly expiry unless fewer than 3 calendar days remain."""
    dates = sorted(set(d for d in dates if d >= today))
    if not dates:
        raise ValueError('No unexpired CRUDEOILM option contracts returned')
    first = dates[0]
    if (first-today).days >= 3:
        return first
    # Require the following calendar month, never a second expiry this month.
    year, month = (first.year+1, 1) if first.month == 12 else (first.year, first.month+1)
    following = [d for d in dates if (d.year, d.month) == (year, month)]
    if not following:
        raise ValueError('Next-month CRUDEOILM options missing; no entry')
    return following[0]


def resolve_crude_feed(broker, today):
    """Discover option expiry and matching mini CRUDEOILM monthly futures."""
    rows = broker.search_options('CRUDEOILM', exchange='MCX')
    option_dates, futures = set(), []
    for row in rows:
        symbol = row.get('tsym', '').upper()
        option = re.fullmatch(r'CRUDEOILM(\d{2}[A-Z]{3}\d{2})[CP]\d+(?:\.\d+)?', symbol)
        future = re.fullmatch(r'CRUDEOILM(\d{2}[A-Z]{3}\d{2})(?:F)?', symbol)
        if option:
            option_dates.add(datetime.strptime(option[1], '%d%b%y').date())
        elif future and row.get('token'):
            expiry = datetime.strptime(future[1], '%d%b%y').date()
            if expiry >= today:
                futures.append((expiry, row))
    chosen = crude_expiry(option_dates, today)
    matching = [(d, row) for d, row in futures if (d.year, d.month) == (chosen.year, chosen.month)]
    if not matching:
        raise ValueError('Matching CRUDEOILM futures month missing from broker search')
    _, row = min(matching, key=lambda item: item[0])
    return {'token': str(row['token']), 'symbol': row['tsym'],
            'option_expiry': chosen.isoformat(), 'date': today.isoformat()}


class OptionSelector:
    def __init__(self, broker, lots=1, market="NIFTY"):
        if type(lots) is not int or lots < 1:
            raise ValueError('Lots must be a positive integer')
        self.broker, self.lots = broker, lots
        self.market = market
        self.root = 'CRUDEOILM' if market == 'CRUDE' else 'NIFTY'
        self.exchange = 'MCX' if market == 'CRUDE' else 'NFO'
        self.expiry_required = None

    def select(self, side, spot, now):
        kind = 'CE' if side == 'LONG' else 'PE'
        strike = (math.ceil(spot/50)-1)*50 if side == 'LONG' else (math.floor(spot/50)+1)*50
        rows = self.broker.search_options(f'{self.root} {strike} {kind}', exchange=self.exchange)
        row, expiry, strike = choose_contract(rows, side, spot, now.date(), self.market, self.expiry_required)
        info = self.broker.security_info(self.exchange, row['token'])
        if info.get('tsym') != row['tsym']:
            raise ValueError('Contract lookup mismatch')
        lot_size = int(info['ls'])
        if lot_size < 1:
            raise ValueError('Broker supplied invalid lot size')
        return dict(exchange=self.exchange, symbol=row['tsym'], token=str(row['token']),
                    product='M' if self.market == 'CRUDE' else 'I', quantity=lot_size*self.lots, action='B',
                    expiry=expiry.isoformat(), strike=strike, lot_size=lot_size)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as file:
            json.dump(self.data, file, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(self.path)


def validate_legs(legs):
    for side in ('LONG', 'SHORT'):
        leg = legs.get(side, {})
        for field in ('exchange', 'symbol', 'product'):
            if not isinstance(leg.get(field), str) or not leg[field].strip():
                raise ValueError(f'{side}: missing {field}')
        if leg.get('action') not in ('B', 'S'):
            raise ValueError(f'{side}: action must be B or S')
        if type(leg.get('quantity')) is not int or leg['quantity'] < 1:
            raise ValueError(f'{side}: quantity must be a positive integer in units')
    return legs


def notify(broker, message):
    """Optional callback supplied by main.py; notification failure cannot halt trades."""
    try:
        sender = getattr(broker, 'notify', None)
        if callable(sender):
            sender(message)
    except Exception:
        print('Telegram notification failed', flush=True)


class StopOrderError(RuntimeError):
    """Stop status is uncertain: retain the position and avoid a second sell."""


class Executor:
    def __init__(self, broker, store, live=False, legs=None, selector=None, fresh_alerts=False):
        self.broker, self.store, self.live = broker, store, live
        self.fresh_alerts = fresh_alerts
        self.selector = selector
        self.legs = validate_legs(legs) if live and selector is None else None
        mode = 'live' if live else 'paper'
        if store.data.get('mode', mode) != mode:
            raise ValueError('Use separate state files for paper and live')
        if fresh_alerts:
            self.archive_attempts('startup: previous attempts retained without retry', preserve_positions=True)
        elif store.data.get('pending'):
            self.recover_pending()
        store.data['mode'] = mode
        store.data.setdefault('strategies', {})
        store.data.setdefault('events', [])
        store.save()

    def archive_attempts(self, reason, preserve_positions=False, strategy_name=None):
        """Retain unresolved intents and exposed positions without resubmission."""
        pending = self.store.data.pop('pending', None)
        unresolved = self.store.data.pop('unresolved_orders', {})
        attempts = list(unresolved.values())
        if pending and pending not in attempts:
            attempts.append(pending)
        for intent in attempts:
            self.store.data.setdefault('order_history', []).append({
                'recorded_at': datetime.now(IST).isoformat(),
                'resolution': 'unconfirmed_no_retry', 'reason': reason, 'intent': intent})
        # An interrupted exit must not be submitted again after a restart.
        for name, state in self.store.data.get('strategies', {}).items():
            if not preserve_positions and name == strategy_name and state.get('close_attempted') and state.get('position'):
                self.store.data.setdefault('unmanaged_positions', []).append({
                    'strategy': name, 'position': state['position'],
                    'reason': 'close outcome unconfirmed; no automatic retry'})
                state['position'] = None
                state.pop('close_attempted', None)
        self.store.save()

    def enter(self, name, side, spot, now):
        if not self.fresh_alerts:
            return self._enter(name, side, spot, now)
        try:
            return self._enter(name, side, spot, now)
        except StopOrderError as error:
            notify(self.broker, f'🚨 {name}: entry recorded but broker SL not confirmed | {error}')
            raise
        except Exception as error:
            self.archive_attempts('entry attempt failed/unconfirmed; wait for a fresh crossover')
            self.store.data.setdefault('attempt_errors', []).append({
                'strategy': name, 'time': now.isoformat(), 'action': 'entry',
                'side': side, 'error_type': type(error).__name__})
            self.store.save()
            notify(self.broker, f'⚠️ {name}: entry failed/unconfirmed; no retry; fresh alerts remain enabled')

    def exit(self, name, spot, now, reason):
        if not self.fresh_alerts:
            if self.live and self.sync_position(name, now):
                return
            return self._exit(name, spot, now, reason)
        state = self.state(name)
        if not state['position']:
            return
        if self.live and self.sync_position(name, now):
            return
        state['close_attempted'] = now.isoformat()
        self.store.save()
        try:
            self._exit(name, spot, now, reason)
        except StopOrderError:
            state.pop('close_attempted', None)
            self.store.save()
            raise
        except Exception as error:
            self.archive_attempts('close attempted once; outcome unconfirmed', strategy_name=name)
            self.store.data.setdefault('attempt_errors', []).append({
                'strategy': name, 'time': now.isoformat(), 'action': 'exit',
                'error_type': type(error).__name__})
            self.store.save()
            notify(self.broker, f'⚠️ {name}: close failed/unconfirmed; exposure may remain; fresh entry allowed')
        else:
            state.pop('close_attempted', None)
            self.store.save()

    def recover_pending(self):
        """Archive proven zero-fill terminal orders; never resubmit or discard history."""
        pending = self.store.data['pending']
        order_id = pending.get('order_id')
        blocked = ('Pending order needs reconciliation; no orders sent. '
                   'Broker must confirm rejected/cancelled status and zero fills.')
        if not self.live or not order_id:
            raise RuntimeError(blocked)
        rows = self.broker.history(order_id)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(blocked)
        leg = pending.get('leg', {})
        terminal = []
        for row in rows:
            if str(row.get('norenordno')) != str(order_id):
                raise RuntimeError(blocked)
            # History may contain older PENDING rows. Any evidence of a fill
            # takes precedence over a terminal row reporting zero fills.
            if row.get('status') == 'COMPLETE':
                raise RuntimeError(blocked)
            for key in ('fillshares', 'avgprc'):
                value = row.get(key)
                if value not in (None, ''):
                    try:
                        number = float(value)
                    except (ValueError, TypeError):
                        raise RuntimeError(blocked)
                    if not math.isfinite(number) or number != 0:
                        raise RuntimeError(blocked)
            if row.get('status') in ('REJECTED', 'CANCELED', 'CANCELLED'):
                # Missing fill quantity is not proof of zero executions.
                if row.get('fillshares') in (None, ''):
                    continue
                if (row.get('tsym') != leg.get('symbol')
                        or row.get('exch') != leg.get('exchange')
                        or row.get('trantype') != pending.get('action')
                        or row.get('prd') != leg.get('product')):
                    raise RuntimeError(blocked)
                terminal.append(row)
        if not terminal:
            raise RuntimeError(blocked)
        # A single atomic save preserves the full intent and broker history
        # while removing only the startup blocker, including for old dates.
        record = {'resolved_at': datetime.now(IST).isoformat(),
                  'resolution': 'terminal_zero_fill', 'order_id': str(order_id),
                  'status': terminal[0]['status'], 'filled_quantity': 0,
                  'reason': terminal[0].get('rejreason', terminal[0].get('emsg', '')),
                  'intent': pending, 'broker_history': rows}
        self.store.data.setdefault('order_history', []).append(record)
        del self.store.data['pending']
        try:
            self.store.save()
        except Exception:
            self.store.data['pending'] = pending
            self.store.data['order_history'].pop()
            raise
        print(f"Archived {record['status']} order {order_id} with zero fills; pending cleared.", flush=True)

    def reconcile(self):
        """Require broker net quantities to match our ledger on managed instruments."""
        if not self.live:
            return
        key = lambda leg: (leg['exchange'], leg['symbol'], leg['product'])
        expected = {key(leg): 0 for leg in (self.legs or {}).values()}
        for state in self.store.data['strategies'].values():
            pos = state.get('position')
            if pos:
                leg = pos['leg']
                if self.selector and (leg['exchange'] != self.selector.exchange or not leg['symbol'].startswith(self.selector.root)):
                    continue
                instrument = key(leg)
                expected[instrument] = expected.get(instrument, 0) + leg['quantity'] * (1 if leg['action'] == 'B' else -1)
        actual = dict.fromkeys(expected, 0)
        for row in self.broker.positions():
            if self.selector and row.get('exch') == self.selector.exchange and row.get('tsym', '').startswith(self.selector.root) and row.get('prd') in ('I', 'M'):
                expected.setdefault((row['exch'], row['tsym'], row['prd']), 0)
                actual.setdefault((row['exch'], row['tsym'], row['prd']), 0)
            instrument = (row.get('exch'), row.get('tsym'), row.get('prd'))
            if instrument in actual:
                actual[instrument] += int(row['netqty'])
        if actual != expected:
            raise RuntimeError('Broker positions differ from strategy ledger; reconcile before running')

    def state(self, name):
        return self.store.data['strategies'].setdefault(name, {
            'position': None, 'previous': None, 'stopped_side': None})

    def submit(self, name, leg, action, reason, entry_order_id=None):
        if self.store.data.get('pending') or name in self.store.data.get('unresolved_orders', {}):
            raise RuntimeError('Unresolved order for this strategy; duplicate submission blocked')
        if leg.get('product') != 'I' and not (leg.get('product') == 'M' and leg.get('exchange') == 'MCX' and leg.get('symbol', '').startswith('CRUDEOILM')):
            raise ValueError('Product M is allowed only for MCX CRUDEOILM; otherwise use I')
        intent = {'strategy': name, 'leg': leg, 'action': action,
                  'created_at': datetime.now(IST).isoformat(),
                  'reason': reason, 'entry_order_id': entry_order_id, 'tag': f'{name}-{uuid.uuid4().hex[:12]}'}
        self.store.data['pending'] = intent
        self.store.save()  # Persist before making a non-idempotent HTTP call.
        result = self.broker.order(leg, action, intent['tag'])
        if not isinstance(result, dict) or result.get('stat') != 'Ok' or not result.get('norenordno'):
            raise RuntimeError('Order not confirmed; reconcile pending intent before continuing')
        intent['order_id'] = result['norenordno']
        self.store.save()
        notify(self.broker, f"📨 Order submitted (not yet filled) | {name} | {action} {leg['symbol']} | qty {leg['quantity']} | {leg['product']} | ID {intent['order_id']} | {datetime.now(IST).isoformat()}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = self.broker.history(intent['order_id'])
            if isinstance(rows, list):
                for row in rows:
                    if row.get('status') == 'COMPLETE' and int(row.get('fillshares', 0)) == leg['quantity']:
                        price = float(row['avgprc'])
                        if not math.isfinite(price) or price <= 0:
                            raise RuntimeError('Invalid broker fill price')
                        notify(self.broker, f"✅ Order filled | {name} | {action} {leg['symbol']} | qty {leg['quantity']} | price {price} | ID {intent['order_id']} | {reason} | {datetime.now(IST).isoformat()}")
                        return {'price': price, 'order_id': intent['order_id']}
                    if row.get('status') in ('REJECTED', 'CANCELED'):
                        notify(self.broker, f"❌ Order {row['status']} | {name} | ID {intent['order_id']} | {row.get('rejreason', 'No reason returned')}")
                        raise RuntimeError('Order rejected/canceled; reconcile any partial fill before restarting')
            time.sleep(1)
        raise RuntimeError('Order still pending or partially filled; runner halted for reconciliation')

    def _enter(self, name, side, spot, now):
        state = self.state(name)
        if state['position']:
            raise RuntimeError('Exit existing strategy position before entry')
        leg = self.selector.select(side, spot, now) if self.selector else (self.legs[side] if self.live else None)
        if self.live and not self.fresh_alerts:
            self.reconcile()
        result = self.submit(name, leg, leg['action'], 'entry') if self.live else None
        fill = result['price'] if result else (self.broker.option_price(leg) if leg else spot)
        entry_order_id = result['order_id'] if result else None
        if not math.isfinite(fill) or fill <= 0:
            raise ValueError('Invalid fill/option quote')
        state['position'] = {'side': side, 'spot_entry': spot, 'fill': fill,
                             'time': now.isoformat(), 'leg': leg, 'entry_order_id': entry_order_id}
        self.store.data.pop('pending', None)
        self.event(name, 'ENTRY', side, spot, now, leg=leg, fill=fill, entry_order_id=entry_order_id)
        if self.live:
            self.protect_position(name, now)

    def verify_entry(self, position):
        """Verify the recorded entry ID; regular intraday exits use offset orders."""
        order_id = position.get('entry_order_id')
        if not order_id:
            raise RuntimeError('Entry order ID missing; reconcile legacy position before exit')
        leg = position['leg']
        if leg.get('product') not in ('I', 'M'):
            raise RuntimeError('Unsupported product for position exit')
        rows = self.broker.history(order_id)
        if isinstance(rows, list):
            for row in rows:
                if (str(row.get('norenordno')) == str(order_id)
                        and row.get('status') == 'COMPLETE'
                        and row.get('tsym') == leg['symbol']
                        and row.get('exch') == leg['exchange']
                        and row.get('prd') == leg['product']
                        and row.get('trantype') == leg['action']
                        and int(row.get('fillshares', 0)) == leg['quantity']):
                    return
        raise RuntimeError('Recorded entry ID does not match confirmed contract/quantity; no exit sent')

    def stop_status(self, position):
        stop = position.get('broker_sl')
        if not stop:
            return None
        if not stop.get('order_id'):
            raise StopOrderError('SL submission not confirmed; check broker before another sell')
        rows = self.broker.history(stop['order_id'])
        if not isinstance(rows, list) or not rows:
            raise StopOrderError('SL order history unavailable')
        matching = [r for r in rows if str(r.get('norenordno')) == str(stop['order_id'])]
        if not matching:
            raise StopOrderError('SL order history ID mismatch')
        # Broker history is newest first; a complete fill takes precedence.
        row = next((r for r in matching if r.get('status') == 'COMPLETE'), matching[0])
        leg = position['leg']
        if (row.get('tsym') != leg['symbol'] or row.get('exch') != leg['exchange']
                or row.get('prd') != leg['product'] or row.get('trantype') != 'S'):
            raise StopOrderError('SL contract/product does not match position')
        filled = int(row.get('fillshares') or 0)
        if filled not in (0, leg['quantity']):
            raise StopOrderError('SL partially filled; reconcile remaining quantity')
        if row.get('status') == 'COMPLETE' and filled != leg['quantity']:
            raise StopOrderError('SL complete quantity mismatch')
        stop['status'] = row.get('status')
        stop['filled_quantity'] = filled
        stop['fill_price'] = row.get('avgprc')
        self.store.save()
        return row

    def release_stop(self, position):
        """Confirm the resting stop is terminal before a discretionary sell."""
        row = self.stop_status(position)
        if row is None:
            return
        terminal = ('COMPLETE', 'CANCELED', 'CANCELLED', 'REJECTED')
        if row.get('status') in terminal:
            return
        stop = position['broker_sl']
        if stop.get('cancel_requested'):
            raise StopOrderError('SL cancellation still unconfirmed; no second sell sent')
        stop['cancel_requested'] = True
        self.store.save()
        try:
            self.broker.cancel_stop(stop['order_id'])
        except Exception as error:
            raise StopOrderError('SL cancellation response lost; check order status') from error
        row = self.stop_status(position)
        if row.get('status') not in terminal:
            raise StopOrderError('Waiting for SL cancellation/fill confirmation')

    def protect_position(self, name, now):
        state = self.state(name)
        pos = state['position']
        if not self.live or not pos:
            return
        points = CRUDE_SL_POINTS if pos['leg']['exchange'] == 'MCX' else NIFTY_SL_POINTS
        if points <= 0:
            return
        if pos.get('broker_sl'):
            row = self.stop_status(pos)
            if row['status'] in ('REJECTED', 'CANCELED', 'CANCELLED'):
                if datetime.fromisoformat(pos['broker_sl']['created_at']).date() < now.date() and int(row.get('fillshares') or 0) == 0:
                    pos.setdefault('sl_history', []).append(pos.pop('broker_sl'))
                    self.store.save()
                else:
                    raise StopOrderError('Position has no active broker SL; previous stop rejected/cancelled')
            else:
                return
        leg = pos['leg']
        info = self.broker.security_info(leg['exchange'], leg['token'])
        if info.get('tsym') != leg['symbol']:
            raise StopOrderError('SL tick-size lookup contract mismatch')
        tick = Decimal(str(info['ti']))
        if not tick.is_finite() or tick <= 0:
            raise StopOrderError('Invalid broker tick size')
        trigger = ((Decimal(str(pos['fill']))-Decimal(str(points)))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
        limit = ((trigger-Decimal(str(SL_LIMIT_BUFFER)))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
        if limit <= 0:
            raise StopOrderError('SL distance/buffer exceeds option premium; position not protected')
        stop = {'trigger': float(trigger), 'limit': float(limit), 'points': points,
                'tag': f'sl-{uuid.uuid4().hex[:12]}', 'created_at': now.isoformat(), 'status': 'SUBMITTING'}
        pos['broker_sl'] = stop
        self.store.save()  # Never blindly resubmit after an uncertain response.
        try:
            result = self.broker.stop_order(leg, float(trigger), float(limit), stop['tag'])
        except Exception as error:
            raise StopOrderError('SL submission response lost; position remains recorded') from error
        if not isinstance(result, dict) or result.get('stat') != 'Ok' or not result.get('norenordno'):
            stop['status'] = 'SUBMISSION_FAILED'
            self.store.save()
            raise StopOrderError('Broker did not accept SL; position remains recorded and unprotected')
        stop['order_id'] = str(result['norenordno'])
        self.store.save()
        row = self.stop_status(pos)
        if row.get('status') not in ('TRIGGER_PENDING', 'OPEN', 'COMPLETE'):
            raise StopOrderError(f"SL not confirmed active: {row.get('status')}")
        notify(self.broker, f"🛡️ {name} SL {row['status']} | {leg['symbol']} | trigger {trigger} | limit {limit} | ID {stop['order_id']}")

    def sync_position(self, name, now, own_exit=False):
        """Check the recorded contract/product; never assume an API failure is flat."""
        state = self.state(name)
        position = state['position']
        if not self.live or not position:
            return False
        leg = position['leg']
        rows = self.broker.positions()
        if not isinstance(rows, list):
            raise RuntimeError('Position lookup unavailable; no exit sent')
        quantity = sum(int(row['netqty']) for row in rows
                       if row.get('exch') == leg['exchange']
                       and row.get('tsym') == leg['symbol']
                       and row.get('prd') == leg['product'])
        if quantity == 0:
            if position.get('broker_sl'):
                self.release_stop(position)
            stop = position.get('broker_sl', {})
            stopped = stop.get('status') == 'COMPLETE'
            if stopped:
                state['stopped_side'] = position['side']
                notify(self.broker, f"🛑 {name}: broker SL filled | {leg['symbol']} | ID {stop['order_id']} | price {stop.get('fill_price')}")
            self.store.data.setdefault('position_history', []).append({
                'strategy': name, 'time': now.isoformat(), 'position': position,
                'status': 'broker_sl_filled' if stopped else 'externally_closed', 'exit_price': stop.get('fill_price')})
            state['position'] = None
            state.pop('close_attempted', None)
            state['baseline_required'] = True
            self.store.save()
            notify(self.broker, f"ℹ️ {name}: broker confirms {leg['symbol']} is flat; recorded as external/manual close. Waiting for a fresh crossover.")
            return True
        expected = leg['quantity'] * (1 if leg['action'] == 'B' else -1)
        if quantity != expected:
            raise RuntimeError(f'{name}: broker quantity {quantity} differs from recorded {expected}; no automatic exit sent')
        if state.get('close_attempted') and not own_exit:
            raise RuntimeError(f'{name}: prior exit outcome uncertain; position retained, no duplicate exit sent')
        return False

    def _exit(self, name, spot, now, reason):
        state = self.state(name)
        pos = state['position']
        if not pos:
            return
        exit_order_id = None
        if self.live:
            if not self.fresh_alerts:
                self.reconcile()
            leg = pos['leg']
            if pos.get('broker_sl'):
                self.release_stop(pos)
                if self.sync_position(name, now, own_exit=True):
                    return
                if pos['broker_sl'].get('status') == 'COMPLETE':
                    raise StopOrderError('Stop filled but position book is not flat yet; no extra sell')
            self.verify_entry(pos)
            result = self.submit(name, leg, 'S' if leg['action'] == 'B' else 'B', reason,
                                 entry_order_id=pos['entry_order_id'])
            fill, exit_order_id = result['price'], result['order_id']
        else:
            fill = self.broker.option_price(pos['leg']) if pos['leg'] else spot
        state['position'] = None
        self.store.data.pop('pending', None)
        self.event(name, 'EXIT', pos['side'], spot, now, reason, leg=pos['leg'], fill=fill,
                   entry_order_id=pos.get('entry_order_id'), exit_order_id=exit_order_id)

    def event(self, name, event, side, spot, now, reason='', leg=None, fill=None, entry_order_id=None, exit_order_id=None):
        record = dict(strategy=name, event=event, side=side, spot=spot,
                      time=now.isoformat(), reason=reason, leg=leg, fill=fill,
                      entry_order_id=entry_order_id, exit_order_id=exit_order_id)
        self.store.data['events'].append(record)
        self.store.save()
        print(json.dumps(record), flush=True)


# Internal context, set separately for each market by the sequential runner.
MARKET = 'NIFTY'
IST = ZoneInfo('Asia/Kolkata')
ROOT = Path(__file__).resolve().parent
ENTRY_START = dtime(9, 45)
EXIT_TIME = dtime(15, 13)  # First minute poll after the strategy's 15:12:59 cutoff.


def normalize(rows, now):
    candles = {}
    for row in rows:
        if 'ssboe' in row:
            dt = datetime.fromtimestamp(int(row['ssboe']), IST)
        else:
            dt = datetime.strptime(row['time'], '%d-%m-%Y %H:%M:%S').replace(tzinfo=IST)
        if dt > now:
            continue
        values = {k: float(row[src]) for k, src in
                  [('open', 'into'), ('high', 'inth'), ('low', 'intl'), ('close', 'intc')]}
        if not all(math.isfinite(v) and v > 0 for v in values.values()):
            raise ValueError('Invalid OHLC data')
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise ValueError('Inconsistent OHLC data')
        candles[dt] = {'datetime': dt.isoformat(), **values}
    return [candles[t] for t in sorted(candles)]


def timeframe_for(market):
    return CRUDE_TIMEFRAME_MINUTES if market == 'CRUDE' else NIFTY_TIMEFRAME_MINUTES


def fresh_forming(candles, now, timeframe=None):
    if not candles:
        return False
    dt = datetime.fromisoformat(candles[-1]['datetime'])
    return dt.date() == now.date() and timedelta(0) <= now-dt < timedelta(minutes=timeframe_for(MARKET) if timeframe is None else timeframe)


def fetch_strategy_candles(broker, token, exchange, market, start, now):
    """Return completed bars only; never reconstruct or trade a forming bar."""
    interval = timeframe_for(market)
    rows = broker.candles(token, start, int(now.timestamp()), exchange=exchange, interval=interval)
    candles = normalize(rows, now)
    closed = [c for c in candles if datetime.fromisoformat(c['datetime'])+timedelta(minutes=interval) <= now]
    latest = datetime.fromisoformat(closed[-1]['datetime'])+timedelta(minutes=interval) if closed else None
    if latest is None or now-latest > timedelta(minutes=interval, seconds=120):
        # Retry a narrow recent request when broad history is delayed/capped.
        day_start = now.replace(hour=9, minute=0 if market == 'CRUDE' else 15, second=0, microsecond=0)
        recent = normalize(broker.candles(token, int(day_start.timestamp()), int(now.timestamp()),
                                         exchange=exchange, interval=interval), now)
        merged = {c['datetime']: c for c in candles}
        merged.update({c['datetime']: c for c in recent})
        closed = [merged[t] for t in sorted(merged)
                  if datetime.fromisoformat(t)+timedelta(minutes=interval) <= now]
        latest = datetime.fromisoformat(closed[-1]['datetime'])+timedelta(minutes=interval) if closed else None
    if latest is None or now-latest > timedelta(minutes=interval, seconds=120):
        print(f'{market}: poll={now.isoformat()} latest_closed={latest} interval={interval}', flush=True)
        raise ValueError('Recent completed candle unavailable; signals skipped; broker SL remains active independently')
    return closed


def closed_observation(strategy, executor, candles):
    """Consume each completed bar once, using consecutive closed WT readings."""
    state = executor.state(strategy.name)
    stamp = candles[-1]['datetime']
    if state.get('last_closed_candle') and stamp <= state['last_closed_candle']:
        return None
    observation = strategy.observe(candles, None)
    state['last_closed_candle'] = stamp
    if state.pop('baseline_required', False):
        observation['signal'] = None
    state['previous'] = observation['value']
    executor.store.save()  # A failed entry must not repeat this candle's alert.
    return observation


def manage(strategy, executor, observation, spot, now, allow_entry=True):
    state = executor.state(strategy.name)
    # Consume this observation before any broker action. A failed request must
    # not turn the same crossover into another entry attempt on the next poll.
    if observation:
        state['previous'] = observation['value']
        executor.store.save()
    position = state['position']
    if position and (datetime.fromisoformat(position['time']).date() < now.date() or now.time() >= EXIT_TIME):
        executor.exit(strategy.name, spot, now, 'session square-off')
        return
    signal = observation['signal'] if observation else None
    if signal and signal != state['stopped_side']:
        state['stopped_side'] = None
    if position and not executor.live:
        # Paper mode models premium SL; live protection rests at the broker.
        if position.get('leg'):
            adverse = position['fill'] - executor.broker.option_price(position['leg'])
        else:
            adverse = position['spot_entry']-spot if position['side'] == 'LONG' else spot-position['spot_entry']
        sl_points = CRUDE_SL_POINTS if MARKET == 'CRUDE' else NIFTY_SL_POINTS
        if sl_points and adverse >= sl_points:
            side = position['side']
            executor.exit(strategy.name, spot, now, f'{MARKET} premium SL')
            state['stopped_side'] = None if signal and signal != side else side
            position = None
    if signal and position and position['side'] != signal:
        executor.exit(strategy.name, spot, now, 'WT1 reverse crossing')
        position = None
    if state.get('baseline_required'):
        allow_entry = False  # Manual close detected during the exit precheck: wait for a new crossover.
    if signal and allow_entry and ENTRY_START <= now.time() < EXIT_TIME:
        if not position and state['stopped_side'] != signal:
            executor.enter(strategy.name, signal, spot, now)
    if observation:
        state['previous'] = observation['value']
    executor.store.save()


def cycle(broker, executor, strategies, token, now, allow_entry=True):
    # Exit overdue positions even if historical candle retrieval fails.
    for strategy in strategies:
        pos = executor.state(strategy.name)['position']
        if pos and (now.time() >= EXIT_TIME or datetime.fromisoformat(pos['time']).date() < now.date()):
            executor.exit(strategy.name, None, now, 'session square-off')
    if now.weekday() >= 5 or not (dtime(9, 0) if MARKET == 'CRUDE' else dtime(9, 15)) <= now.time() < EXIT_TIME:
        return
    exchange = 'MCX' if MARKET == 'CRUDE' else 'NSE'
    if MARKET == 'CRUDE':
        feed = executor.store.data.get('crude_feed')
        legacy_feed = feed and not re.fullmatch(r'CRUDEOILM\d{2}[A-Z]{3}\d{2}(?:F)?', feed['symbol'])
        legacy_open = legacy_feed and any(executor.state(st.name)['position'] for st in strategies)
        if legacy_open:
            # Manage an existing standard contract against its original futures,
            # then switch to mini once flat. Never price its SL using mini data.
            allow_entry = False
        if not legacy_open and (not feed or legacy_feed or feed['date'] != now.date().isoformat()):
            new_feed = resolve_crude_feed(broker, now.date())
            if feed and feed['token'] != new_feed['token']:
                if any(executor.state(st.name)['position'] for st in strategies):
                    raise RuntimeError('Cannot change crude feed with an open position')
                for st in strategies:
                    executor.state(st.name).update(previous=None, stopped_side=None)
                executor.store.data['history_start'] = int((now-timedelta(days=max(14, math.ceil(timeframe_for(MARKET)*100/300)*2))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            executor.store.data['crude_feed'] = feed = new_feed
            executor.store.save()
        token = feed['token']
        executor.selector.expiry_required = datetime.fromisoformat(feed['option_expiry']).date()
    quote = broker.quote(token, exchange=exchange)
    spot = float(quote['lp'])
    if not math.isfinite(spot) or spot <= 0:
        raise ValueError('Invalid underlying quote')
    if quote.get('ft') and abs(now.timestamp()-float(quote['ft'])) > 90:
        raise ValueError('Stale underlying quote')
    # Risk checks run even when candle retrieval is unavailable.
    for strategy in strategies:
        manage(strategy, executor, None, spot, now, allow_entry=allow_entry)
    start = int(executor.store.data['history_start'])
    candles = fetch_strategy_candles(broker, token, exchange, MARKET, start, now)
    for strategy in strategies:
        state = executor.state(strategy.name)
        observation = closed_observation(strategy, executor, candles)
        if observation is None:
            continue
        manage(strategy, executor, observation, spot, now, allow_entry=allow_entry)
        print(f"{now.isoformat()} {strategy.name} {MARKET}={spot} WT1={observation['value']} signal={observation['signal']}", flush=True)




SETTINGS_NAMES = (
    'NIFTY', 'NIFTY_START_TIME', 'NIFTY_END_TIME',
    'CRUDE', 'CRUDE_START_TIME', 'CRUDE_END_TIME',
    'NIFTY_TIMEFRAME_MINUTES', 'CRUDE_TIMEFRAME_MINUTES', 'CHANNEL_LENGTH', 'AVERAGE_LENGTH', 'SIGNAL_LENGTH', 'NIFTY_SL_POINTS', 'CRUDE_SL_POINTS', 'SL_LIMIT_BUFFER',
)


def read_settings(path=None):
    """Read only literal settings, never execute/reimport the running strategy."""
    tree = ast.parse(Path(path or __file__).read_text())
    settings = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in SETTINGS_NAMES:
                continue
            value = node.value
            if target.id.endswith('_TIME'):
                if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != 'dtime' or value.keywords:
                    raise ValueError(f'{target.id} must use dtime(hour, minute[, second])')
                value = dtime(*(ast.literal_eval(arg) for arg in value.args))
            else:
                value = ast.literal_eval(value)
            settings[target.id] = value
    if set(settings) != set(SETTINGS_NAMES):
        raise ValueError('Missing editable settings in wt_cross.py')
    for market in ('NIFTY', 'CRUDE'):
        if type(settings[market]) is not bool:
            raise ValueError(f'{market} must be True or False')
        if settings[f'{market}_START_TIME'] >= settings[f'{market}_END_TIME']:
            raise ValueError(f'{market} start must be before end')
    for name in ('NIFTY_TIMEFRAME_MINUTES', 'CRUDE_TIMEFRAME_MINUTES'):
        if type(settings[name]) is not int or settings[name] not in (1, 3, 5, 10, 15, 30, 60, 120, 240):
            raise ValueError(f'Unsupported timeframe: {name}')
    for name in ('CHANNEL_LENGTH', 'AVERAGE_LENGTH', 'SIGNAL_LENGTH'):
        if type(settings[name]) is not int or settings[name] < 1:
            raise ValueError(f'{name} must be a positive integer')
    for name in ('NIFTY_SL_POINTS', 'CRUDE_SL_POINTS', 'SL_LIMIT_BUFFER'):
        sl = settings[name]
        if type(sl) not in (int, float) or not math.isfinite(sl) or sl < 0:
            raise ValueError(f'{name} must be finite and nonnegative')
    return settings


def apply_settings(settings, store):
    snapshot = {k: v.isoformat() if isinstance(v, dtime) else v for k,v in settings.items()}
    prior = store.data.get('settings', {})
    indicator_keys = ('CHANNEL_LENGTH', 'AVERAGE_LENGTH', 'SIGNAL_LENGTH')
    for market in ('NIFTY', 'CRUDE'):
        keys = (*indicator_keys, f'{market}_TIMEFRAME_MINUTES')
        if prior and any(prior.get(k) != snapshot[k] for k in keys):
            state = store.data.get('strategies', {}).get(f'wt_cross_{market.lower()}')
            if state is not None:
                state['previous'] = None
                state.pop('last_closed_candle', None)
                state['baseline_required'] = True
    globals().update(settings)
    if prior != snapshot:
        print('Updated settings:', json.dumps(snapshot), flush=True)
        store.data['settings'] = snapshot
        store.save()


def park_pending(store):
    """Move a single in-flight intent into the per-strategy unresolved queue."""
    pending = store.data.get('pending')
    if not pending:
        return
    name = pending.get('strategy')
    if name not in ('wt_cross_nifty', 'wt_cross_crude'):
        raise RuntimeError('Pending order has unknown strategy; manual reconciliation required')
    unresolved = store.data.setdefault('unresolved_orders', {})
    if name in unresolved and unresolved[name] != pending:
        raise RuntimeError('Conflicting unresolved orders; refusing to overwrite records')
    unresolved[name] = pending
    del store.data['pending']
    store.save()


def check_unresolved(executor, name):
    store = executor.store
    pending = store.data.get('unresolved_orders', {}).get(name)
    if not pending:
        return
    now = datetime.now(IST)
    created = pending.get('created_at')
    if created:
        submitted = datetime.fromisoformat(created)
        if submitted.tzinfo is None:
            raise RuntimeError('Pending order timestamp has no timezone; verify its date')
        if submitted.astimezone(IST).date() < now.date():
            # Date rollover is not evidence of a rejection or zero fills.
            # Preserve the unknown outcome; normal position reconciliation below
            # still blocks entries when broker exposure differs from our ledger.
            record = {'resolved_at': now.isoformat(),
                      'resolution': 'prior_day_archived_status_unverified',
                      'order_id': pending.get('order_id'), 'intent': pending}
            store.data.setdefault('order_history', []).append(record)
            del store.data['unresolved_orders'][name]
            try:
                store.save()
            except Exception:
                store.data['unresolved_orders'][name] = pending
                store.data['order_history'].pop()
                raise
            notify(executor.broker, f"ℹ️ {name}: prior-day order {pending.get('order_id')} archived; broker positions will still be reconciled")
            return
    else:
        raise RuntimeError('Legacy pending order has no submission date; verify and add created_at with IST timezone. Record retained.')
    # Recovery is sequential under the runner lock. Leave the queue entry on
    # disk until recovery succeeds, so a crash cannot lose the order record.
    store.data['pending'] = pending
    try:
        executor.recover_pending()
    except Exception:
        store.data.pop('pending', None)
        raise
    else:
        del store.data['unresolved_orders'][name]
        store.save()


def scan_while_blocked(broker, executor, strategy, market, nifty_token, now):
    """Keep tracking crossovers, but never queue alerts or send orders."""
    if now.weekday() >= 5 or now.time() >= EXIT_TIME:
        return
    if market == 'CRUDE':
        feed = executor.store.data.get('crude_feed')
        if not feed:
            return
        token, exchange = feed['token'], 'MCX'
    else:
        token, exchange = nifty_token, 'NSE'
    candles = fetch_strategy_candles(broker, token, exchange, market, executor.store.data['history_start'], now)
    state = executor.state(strategy.name)
    observation = closed_observation(strategy, executor, candles)
    if observation is None:
        return
    print(f"{now.isoformat()} {strategy.name} scanning: WT1={observation['value']} "
          f"signal={observation['signal']} | orders blocked pending reconciliation; alert not queued", flush=True)


def run(broker, args):
    """Shared session, one loop, independent NIFTY and CRUDE positions."""
    global MARKET, ENTRY_START, EXIT_TIME
    settings = read_settings()
    state_path = args.state or ROOT / ('live_state.json' if args.live else 'paper_state.json')
    with state_path.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(state_path)
        for state in store.data.get('strategies', {}).values():
            state['baseline_required'] = True
        print('Resume: saved positions will be checked against broker positions.', flush=True)
        apply_settings(settings, store)
        park_pending(store)
        last_alerts = {}
        runners = {}
        for market in ('NIFTY', 'CRUDE'):
            strategy = Strategy()
            strategy.name = f'wt_cross_{market.lower()}'
            executor = Executor(broker, store, args.live, selector=OptionSelector(broker, args.lots, market), fresh_alerts=True)
            runners[market] = (strategy, executor)
        while True:
            config_ok = True
            try:
                updated = read_settings()
                apply_settings(updated, store)
                settings = updated
            except (OSError, SyntaxError, ValueError, TypeError) as error:
                config_ok = False
                print(f'Invalid settings ({type(error).__name__}); keeping last valid settings for exits; new entries blocked.', flush=True)
            now = datetime.now(IST)
            anchor = int((now-timedelta(days=max(14, math.ceil(max(NIFTY_TIMEFRAME_MINUTES, CRUDE_TIMEFRAME_MINUTES)*100/300)*2))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            store.data['history_start'] = min(anchor, store.data.get('history_start', anchor))
            store.save()
            poll_errors = []
            for market, (strategy, executor) in runners.items():
                enabled = settings[market] and config_ok
                if not enabled and not executor.state(strategy.name)['position'] and strategy.name not in store.data.get('unresolved_orders', {}):
                    continue
                MARKET = market
                ENTRY_START, EXIT_TIME = settings[f'{market}_START_TIME'], settings[f'{market}_END_TIME']
                try:
                    executor.sync_position(strategy.name, now)
                    recorded = executor.state(strategy.name)['position']
                    if recorded and datetime.fromisoformat(recorded['time']).date() == now.date() and ENTRY_START <= now.time() < EXIT_TIME:
                        try:
                            executor.protect_position(strategy.name, now)
                        except StopOrderError as stop_error:
                            print(f'{market}: broker SL needs attention: {stop_error}; exit checks still run', flush=True)
                    cycle(broker, executor, [strategy], args.nifty_token, now, allow_entry=enabled)
                    executor.protect_position(strategy.name, now)
                    last_alerts.pop(market, None)
                except Exception as error:
                    if store.data.get('pending'):
                        park_pending(store)
                    try:
                        scan_while_blocked(broker, executor, strategy, market, args.nifty_token, now)
                    except Exception as scan_error:
                        print(f'{market} signal scan unavailable: {type(scan_error).__name__}', flush=True)
                    detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Check connection/session'
                    print(f'{market} poll failed ({type(error).__name__}): {detail}; other market continues.', flush=True)
                    previous_alert = last_alerts.get(market)
                    if previous_alert is None or previous_alert[0] != detail or time.monotonic()-previous_alert[1] >= 900:
                        notify(broker, f'⚠️ {market} poll paused/error | {detail} | other strategy continues')
                        last_alerts[market] = (detail, time.monotonic())
                    poll_errors.append(f'{market}: {detail}')
            if args.once:
                if poll_errors:
                    raise RuntimeError('One-shot poll failed: ' + '; '.join(poll_errors))
                break
            time.sleep(max(.1, 60-time.time()%60))


def check_connection(broker, args):
    """Read-only diagnostic: no Executor, no orders and no position mutations."""
    settings = read_settings()
    now = datetime.now(IST)
    failed = False
    for market in ('NIFTY', 'CRUDE'):
        if not settings[market]:
            print(f'{market}: disabled; skipped')
            continue
        try:
            interval = settings[f'{market}_TIMEFRAME_MINUTES']
            days = max(14, math.ceil(interval*100/300)*2)
            start = int((now-timedelta(days=days)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
            selector = OptionSelector(broker, args.lots, market)
            if market == 'CRUDE':
                feed = resolve_crude_feed(broker, now.date())
                token, exchange, symbol = feed['token'], 'MCX', feed['symbol']
                selector.expiry_required = datetime.fromisoformat(feed['option_expiry']).date()
            else:
                token, exchange, symbol = args.nifty_token, 'NSE', 'NIFTY'
            price = float(broker.quote(token, exchange=exchange)['lp'])
            if not math.isfinite(price) or price <= 0:
                raise ValueError('Invalid underlying price')
            rows = normalize(broker.candles(token,start,int(now.timestamp()),exchange=exchange,interval=interval),now)
            latest = rows[-1] if rows else None
            print(json.dumps({'market':market,'underlying':symbol,'exchange':exchange,'token':token,
                              'price':price,'timeframe':interval,'candles':len(rows),'latest':latest,
                              'forming_candle_available':fresh_forming(rows,now,interval)},indent=2))
            if len(rows) < 100:
                print('Insufficient warm-up: strategy needs 100 candles')
                failed = True
            for side in ('LONG','SHORT'):
                leg = selector.select(side,price,now)
                premium = broker.option_price(leg)
                print(json.dumps({'preview_only':True,'signal':side,'option':leg,'premium':premium},indent=2))
        except Exception as error:
            failed = True
            detail = str(error) if isinstance(error,(ValueError,RuntimeError)) else 'Check broker connection/session'
            print(f'{market}: CHECK FAILED ({type(error).__name__}): {detail}')
    print('Read-only check finished. No orders placed or state files changed.')
    if failed:
        raise SystemExit(1)
