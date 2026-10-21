#!/usr/bin/env python3
"""Login/session owner. Start with: python3 2027/main.py --lots 1
Default is LIVE trading; use --paper for simulation or --check for read-only diagnostics.
Fill in credentials.py before running. No environment variables or .env file are required.
Dependencies: requests, pyotp, websocket-client.
"""
import argparse
import json
from pathlib import Path

# Login credentials — fill these in here.
from credentials import USER_ID, PASSWORD, TOTP_SECRET, APP_KEY



TELEGRAM = True


def send_telegram_message(msg, imp=True):
    if not TELEGRAM:
        return False
    try:
        import requests
        BOT_TOKEN = '8331147432:AAGSG4mI8d87sWEBsY0qtarAtwWbpa4viq0'
        CHANNEL_ID = '-1003494200670'
        CHANNEL_ID_IMP = '-1003448158591'
        # Never expose authentication values in an alert.
        for secret in (USER_ID, PASSWORD, TOTP_SECRET, APP_KEY, BOT_TOKEN):
            if secret:
                msg = msg.replace(str(secret), '[REDACTED]')
        response = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                 data={'chat_id': CHANNEL_ID_IMP if imp else CHANNEL_ID,
                                       'text': msg[:4000]}, timeout=10)
        response.raise_for_status()
        return response.json().get('ok') is True
    except Exception as error:
        print('Telegram failed:', type(error).__name__, flush=True)
        return False


def create_api():
    from NorenWebApi import NorenWebApi

    class Broker(NorenWebApi):
        def request(self, route, values):
            config = self._NorenWebApi__service_configweb
            response = self.session.post(
                config['host'] + route,
                # Noren Web expects raw jData JSON, matching NorenWebApi.py.
                # requests' dict form-encoding is rejected by this endpoint.
                data='jData=' + json.dumps({'uid': self.userid, **values}) + '&jKey=' + self.jkey,
                timeout=(5, 15))
            response.raise_for_status()
            return response.json()

        def candles(self, token, start, end, exchange="NSE", interval=15):
            rows = self.request('/TPSeries', {'ordersource': 'API', 'exch': exchange,
                                'token': token, 'st': str(start), 'et': str(end), 'intrv': str(interval)})
            if not isinstance(rows, list) or not rows:
                message = str(rows.get('emsg', 'no candles')) if isinstance(rows, dict) else 'no candles'
                for secret in (self.userid, self.jkey):
                    if secret:
                        message = message.replace(secret, '[REDACTED]')
                raise RuntimeError(f'TPSeries history unavailable: {message[:200]}')
            return rows

        def quote(self, token, exchange="NSE"):
            result = self.request('/GetQuotes', {'exch': exchange, 'token': token})
            if not isinstance(result, dict) or result.get('stat') != 'Ok':
                raise RuntimeError('Underlying quote request failed')
            return result

        def order(self, leg, action, tag):
            if leg.get('product') != 'I' and not (leg.get('product') == 'M' and leg.get('exchange') == 'MCX' and leg.get('symbol', '').startswith('CRUDEOILM')):
                raise ValueError('Product M is allowed only for MCX CRUDEOILM; otherwise use I')
            return self.request('/PlaceOrder', {
                'ordersource': 'API', 'actid': self.userid, 'trantype': action,
                'prd': leg['product'], 'exch': leg['exchange'], 'tsym': leg['symbol'],
                'qty': str(leg['quantity']), 'prctyp': 'MKT', 'prc': '0',
                'ret': 'DAY', 'remarks': tag})

        def stop_order(self, leg, trigger, limit, tag):
            if leg.get('action') != 'B' or leg.get('product') not in ('I', 'M'):
                raise ValueError('Stop orders require a recorded long option position')
            if not 0 < limit <= trigger:
                raise ValueError('Invalid sell-stop limit/trigger')
            return self.request('/PlaceOrder', {
                'ordersource': 'API', 'actid': self.userid, 'trantype': 'S',
                'prd': leg['product'], 'exch': leg['exchange'], 'tsym': leg['symbol'],
                'qty': str(leg['quantity']), 'prctyp': 'SL-LMT',
                'prc': str(limit), 'trgprc': str(trigger), 'ret': 'DAY', 'remarks': tag})

        def cancel_stop(self, order_id):
            return self.request('/CancelOrder', {'ordersource': 'API', 'norenordno': order_id})

        def history(self, order_id):
            return self.request('/SingleOrdHist', {'ordersource': 'API', 'norenordno': order_id})

        def positions(self):
            result = self.request('/PositionBook', {'actid': self.userid})
            if isinstance(result, dict):
                message = ' '.join(str(result.get('emsg', '')).lower().split())
                # PositionBook uses this wrapped error for an empty position book.
                if message in ('no data', 'error occurred : 5 "no data"'):
                    return []
            if not isinstance(result, list):
                message = str(result.get('emsg', 'Unexpected PositionBook response')) if isinstance(result, dict) else 'Unexpected PositionBook response type'
                for secret in (self.userid, self.jkey):
                    if secret:
                        message = message.replace(secret, '[REDACTED]')
                raise RuntimeError(f'Could not reconcile broker positions: {message[:200]}')
            return result

        def search_options(self, text, exchange="NFO"):
            result = self.request('/SearchScrip', {'exch': exchange, 'stext': text})
            if not isinstance(result, dict) or result.get('stat') != 'Ok' or not result.get('values'):
                raise RuntimeError('Option search failed')
            return result['values']

        def security_info(self, exchange, token):
            result = self.request('/GetSecurityInfo', {'exch': exchange, 'token': token})
            if not isinstance(result, dict) or result.get('stat') != 'Ok':
                raise RuntimeError('Security info lookup failed')
            return result

        def option_price(self, leg):
            result = self.request('/GetQuotes', {'exch': leg['exchange'], 'token': leg['token']})
            if not isinstance(result, dict) or result.get('stat') != 'Ok':
                raise RuntimeError('Option quote failed')
            return float(result['lp'])

    return Broker()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--live', dest='live', action='store_true', help='Live trading (default)')
    modes.add_argument('--paper', dest='live', action='store_false', help='Paper simulation; no real orders')
    parser.set_defaults(live=None)
    parser.add_argument('--lots', type=int, default=1, help='Number of option lots (default 1); lot size comes from broker')
    parser.add_argument('--nifty-token', default='26000', help='NSE NIFTY index token; verify with broker')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true', help='Read-only candle/quote/option diagnostics; no orders or state changes')
    args = parser.parse_args()
    if args.check and args.live is True:
        parser.error('--check cannot be combined with --live')
    args.live = False if args.check else (True if args.live is None else args.live)
    print('Mode: ' + ('READ-ONLY CHECK' if args.check else 'LIVE — real orders enabled' if args.live else 'PAPER — simulated orders'), flush=True)
    if args.lots < 1:
        parser.error('--lots must be positive')
    credentials = {
        'userid': USER_ID,
        'password': PASSWORD,
        'totp_secret': TOTP_SECRET,
        'app_key': APP_KEY,
    }
    if not all(credentials.values()):
        parser.error('Fill USER_ID, PASSWORD, TOTP_SECRET and APP_KEY in credentials.py')
    send_telegram_message('▶️ main.py started')
    try:
        api = create_api()
        api.notify = send_telegram_message
        api.login(**credentials)  # One session shared with strategy files.
    except Exception as error:
        send_telegram_message(f'❌ Login/startup failed | {type(error).__name__}')
        raise
    send_telegram_message('✅ Login successful')
    import wt_cross
    try:
        if args.check:
            wt_cross.check_connection(api, args)
        else:
            wt_cross.run(api, args)
    except Exception as error:
        detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
        send_telegram_message(f'❌ Runner stopped | {detail}')
        raise
    except KeyboardInterrupt:
        send_telegram_message('⏹ Runner stopped manually')
        raise


if __name__ == '__main__':
    main()
