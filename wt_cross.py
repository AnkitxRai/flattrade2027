"""Self-contained WaveTrend strategy: candles, ITM options, orders and state.

Call run(api, args) from main.py; this file never logs in.
NIFTY/CRUDE flags, their start/end times, timeframe and indicator/SL settings
are reread from this file every minute. Both markets can run together.
WT1 uses the forming candle, recalculated every minute.
LONG buys the closest strictly ITM call; SHORT buys the closest ITM put.
NIFTY rolls at <=2 inclusive weekdays, matching rmi.py. CRUDE rolls to
next month with <3 calendar days left; candles use that month's futures.
Default: 1 lot, 50 underlying-point SL. NIFTY exit 15:13, CRUDE exit 23:00 IST.
CRUDE uses mini CRUDEOILM options and futures. Missing discovery data blocks
entries; no expiry dates, contract tokens or lot sizes are hardcoded.
Stops are polled once per minute, not broker-resting stops. Unknown order
outcomes halt for reconciliation. Runtime JSON/lock files preserve state.
Formula adapted from WaveTrend with Crosses [LazyBear].
"""
import ast
from collections import deque
from datetime import datetime, timedelta, time as dtime
import fcntl
import json
import math
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
NIFTY_SL_POINTS = 50.0  # NIFTY index points; 0 disables SL

CRUDE = True
CRUDE_START_TIME = dtime(9, 30)
CRUDE_END_TIME = dtime(23, 0)
CRUDE_SL_POINTS = 50.0  # CRUDEOILM futures points; 0 disables SL

TIMEFRAME_MINUTES = 15  # 1, 3, 5, 10, 15, 30, 60, 120, 240
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
                    product='I', quantity=lot_size*self.lots, action='B',
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


class Executor:
    def __init__(self, broker, store, live=False, legs=None, selector=None):
        self.broker, self.store, self.live = broker, store, live
        self.selector = selector
        self.legs = validate_legs(legs) if live and selector is None else None
        mode = 'live' if live else 'paper'
        if store.data.get('mode', mode) != mode:
            raise ValueError('Use separate state files for paper and live')
        if store.data.get('pending'):
            raise RuntimeError('Unresolved order in state file: reconcile broker fills before restarting; do not delete the state to retry')
        store.data['mode'] = mode
        store.data.setdefault('strategies', {})
        store.data.setdefault('events', [])
        store.save()

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
                instrument = key(leg)
                expected[instrument] = expected.get(instrument, 0) + leg['quantity'] * (1 if leg['action'] == 'B' else -1)
        actual = dict.fromkeys(expected, 0)
        for row in self.broker.positions():
            if self.selector and row.get('exch') == self.selector.exchange and row.get('tsym', '').startswith(self.selector.root) and row.get('prd') == 'I':
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
        if leg.get('product') != 'I':
            raise ValueError('Only intraday product I is allowed')
        intent = {'strategy': name, 'leg': leg, 'action': action,
                  'reason': reason, 'entry_order_id': entry_order_id, 'tag': f'{name}-{uuid.uuid4().hex[:12]}'}
        self.store.data['pending'] = intent
        self.store.save()  # Persist before making a non-idempotent HTTP call.
        result = self.broker.order(leg, action, intent['tag'])
        if not isinstance(result, dict) or result.get('stat') != 'Ok' or not result.get('norenordno'):
            raise RuntimeError('Order not confirmed; reconcile pending intent before continuing')
        intent['order_id'] = result['norenordno']
        self.store.save()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rows = self.broker.history(intent['order_id'])
            if isinstance(rows, list):
                for row in rows:
                    if row.get('status') == 'COMPLETE' and int(row.get('fillshares', 0)) == leg['quantity']:
                        price = float(row['avgprc'])
                        if not math.isfinite(price) or price <= 0:
                            raise RuntimeError('Invalid broker fill price')
                        return {'price': price, 'order_id': intent['order_id']}
                    if row.get('status') in ('REJECTED', 'CANCELED'):
                        raise RuntimeError('Order rejected/canceled; reconcile any partial fill before restarting')
            time.sleep(1)
        raise RuntimeError('Order still pending or partially filled; runner halted for reconciliation')

    def enter(self, name, side, spot, now):
        state = self.state(name)
        if state['position']:
            raise RuntimeError('Exit existing strategy position before entry')
        leg = self.selector.select(side, spot, now) if self.selector else (self.legs[side] if self.live else None)
        if self.live:
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

    def verify_entry(self, position):
        """Verify the recorded entry ID; regular intraday exits use offset orders."""
        order_id = position.get('entry_order_id')
        if not order_id:
            raise RuntimeError('Entry order ID missing; reconcile legacy position before exit')
        leg = position['leg']
        if leg.get('product') != 'I':
            raise RuntimeError('Refusing a non-intraday position exit')
        rows = self.broker.history(order_id)
        if isinstance(rows, list):
            for row in rows:
                if (str(row.get('norenordno')) == str(order_id)
                        and row.get('status') == 'COMPLETE'
                        and row.get('tsym') == leg['symbol']
                        and row.get('exch') == leg['exchange']
                        and row.get('prd') == 'I'
                        and row.get('trantype') == leg['action']
                        and int(row.get('fillshares', 0)) == leg['quantity']):
                    return
        raise RuntimeError('Recorded entry ID does not match confirmed contract/quantity; no exit sent')

    def exit(self, name, spot, now, reason):
        state = self.state(name)
        pos = state['position']
        if not pos:
            return
        exit_order_id = None
        if self.live:
            self.reconcile()
            leg = pos['leg']
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


def fresh_forming(candles, now, timeframe=None):
    if not candles:
        return False
    dt = datetime.fromisoformat(candles[-1]['datetime'])
    return dt.date() == now.date() and timedelta(0) <= now-dt < timedelta(minutes=TIMEFRAME_MINUTES if timeframe is None else timeframe)


def manage(strategy, executor, observation, spot, now, allow_entry=True):
    state = executor.state(strategy.name)
    position = state['position']
    if position and (datetime.fromisoformat(position['time']).date() < now.date() or now.time() >= EXIT_TIME):
        executor.exit(strategy.name, spot, now, 'session square-off')
        return
    signal = observation['signal'] if observation else None
    if signal and signal != state['stopped_side']:
        state['stopped_side'] = None
    if position:
        adverse = position['spot_entry']-spot if position['side'] == 'LONG' else spot-position['spot_entry']
        sl_points = CRUDE_SL_POINTS if MARKET == 'CRUDE' else NIFTY_SL_POINTS
        if sl_points and adverse >= sl_points:
            side = position['side']
            executor.exit(strategy.name, spot, now, f'{MARKET} underlying SL')
            state['stopped_side'] = None if signal and signal != side else side
            position = None
    if signal and position and position['side'] != signal:
        executor.exit(strategy.name, spot, now, 'WT1 reverse crossing')
        position = None
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
                executor.store.data['history_start'] = int((now-timedelta(days=max(14, math.ceil(TIMEFRAME_MINUTES*100/300)*2))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
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
    rows = broker.candles(token, start, int(now.timestamp()), exchange=exchange, interval=TIMEFRAME_MINUTES)
    candles = normalize(rows, now)
    if not fresh_forming(candles, now):
        raise ValueError('Current forming candle missing; skipping signals (SL was checked)')
    for strategy in strategies:
        state = executor.state(strategy.name)
        observation = strategy.observe(candles, state['previous'])
        manage(strategy, executor, observation, spot, now, allow_entry=allow_entry)
        print(f"{now.isoformat()} {strategy.name} {MARKET}={spot} WT1={observation['value']} signal={observation['signal']}", flush=True)




SETTINGS_NAMES = (
    'NIFTY', 'NIFTY_START_TIME', 'NIFTY_END_TIME',
    'CRUDE', 'CRUDE_START_TIME', 'CRUDE_END_TIME',
    'TIMEFRAME_MINUTES', 'CHANNEL_LENGTH', 'AVERAGE_LENGTH', 'SIGNAL_LENGTH', 'NIFTY_SL_POINTS', 'CRUDE_SL_POINTS',
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
    if type(settings['TIMEFRAME_MINUTES']) is not int or settings['TIMEFRAME_MINUTES'] not in (1, 3, 5, 10, 15, 30, 60, 120, 240):
        raise ValueError('Unsupported timeframe')
    for name in ('CHANNEL_LENGTH', 'AVERAGE_LENGTH', 'SIGNAL_LENGTH'):
        if type(settings[name]) is not int or settings[name] < 1:
            raise ValueError(f'{name} must be a positive integer')
    for name in ('NIFTY_SL_POINTS', 'CRUDE_SL_POINTS'):
        sl = settings[name]
        if type(sl) not in (int, float) or not math.isfinite(sl) or sl < 0:
            raise ValueError(f'{name} must be finite and nonnegative')
    return settings


def apply_settings(settings, store):
    snapshot = {k: v.isoformat() if isinstance(v, dtime) else v for k,v in settings.items()}
    prior = store.data.get('settings', {})
    indicator_keys = ('TIMEFRAME_MINUTES', 'CHANNEL_LENGTH', 'AVERAGE_LENGTH', 'SIGNAL_LENGTH')
    if prior and any(prior.get(k) != snapshot[k] for k in indicator_keys):
        # Keep orders, entry prices and SL locks; discard only the old indicator comparison.
        for state in store.data.get('strategies', {}).values():
            state['previous'] = None
    globals().update(settings)
    if prior != snapshot:
        print('Updated settings:', json.dumps(snapshot), flush=True)
        store.data['settings'] = snapshot
        store.save()


def run(broker, args):
    """Shared session, one loop, independent NIFTY and CRUDE positions."""
    global MARKET, ENTRY_START, EXIT_TIME
    settings = read_settings()
    state_path = args.state or ROOT / ('live_state.json' if args.live else 'paper_state.json')
    with state_path.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(state_path)
        states = store.data.setdefault('strategies', {})
        # Migrate the earlier single-market ledger without losing an open trade.
        if 'wt_cross' in states:
            old_market = store.data.get('context', {}).get('market', 'NIFTY').lower()
            key = f'wt_cross_{old_market}'
            if key in states:
                raise ValueError('Conflicting legacy and per-market strategy states')
            states[key] = states.pop('wt_cross')
        if not args.state:
            # Never silently ignore positions in the previous per-timeframe files.
            mode = 'live' if args.live else 'paper'
            for old_path in ROOT.glob(f'{mode}_*_state.json'):
                old = json.loads(old_path.read_text())
                if old.get('pending') or any(st.get('position') for st in old.get('strategies', {}).values()):
                    raise ValueError(f'Open trade/order in legacy state {old_path.name}; reconcile before using the combined runner')
        apply_settings(settings, store)
        runners = {}
        for market in ('NIFTY', 'CRUDE'):
            strategy = Strategy()
            strategy.name = f'wt_cross_{market.lower()}'
            executor = Executor(broker, store, args.live, selector=OptionSelector(broker, args.lots, market))
            if settings[market] or executor.state(strategy.name)['position']:
                executor.reconcile()
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
            anchor = int((now-timedelta(days=max(14, math.ceil(TIMEFRAME_MINUTES*100/300)*2))).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            store.data['history_start'] = min(anchor, store.data.get('history_start', anchor))
            store.save()
            poll_errors = []
            for market, (strategy, executor) in runners.items():
                enabled = settings[market] and config_ok
                if not enabled and not executor.state(strategy.name)['position']:
                    continue
                MARKET = market
                ENTRY_START, EXIT_TIME = settings[f'{market}_START_TIME'], settings[f'{market}_END_TIME']
                try:
                    cycle(broker, executor, [strategy], args.nifty_token, now, allow_entry=enabled)
                except Exception as error:
                    if store.data.get('pending'):
                        raise RuntimeError('Order unresolved; runner stopped for broker reconciliation.') from error
                    detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else 'Check connection/session'
                    print(f'{market} poll failed ({type(error).__name__}): {detail}; other market continues.', flush=True)
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
    interval = settings['TIMEFRAME_MINUTES']
    days = max(14, math.ceil(interval*100/300)*2)
    start = int((now-timedelta(days=days)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())
    failed = False
    for market in ('NIFTY', 'CRUDE'):
        if not settings[market]:
            print(f'{market}: disabled; skipped')
            continue
        try:
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
