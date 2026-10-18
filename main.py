#!/usr/bin/env python3
"""Login/session owner. Start with: python3 2027/main.py --lots 1
Default is paper; add --live for real orders. Fill in the credential constants
below before running. No environment variables or .env file are required.
Dependencies: requests, pyotp, websocket-client.
"""
import argparse
import json
from pathlib import Path

# Login credentials — fill these in here.
from credentials import USER_ID, PASSWORD, TOTP_SECRET, APP_KEY



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
            if leg.get('product') != 'I':
                raise ValueError('Only intraday product I is allowed')
            return self.request('/PlaceOrder', {
                'ordersource': 'API', 'actid': self.userid, 'trantype': action,
                'prd': 'I', 'exch': leg['exchange'], 'tsym': leg['symbol'],
                'qty': str(leg['quantity']), 'prctyp': 'MKT', 'prc': '0',
                'ret': 'DAY', 'remarks': tag})

        def history(self, order_id):
            return self.request('/SingleOrdHist', {'ordersource': 'API', 'norenordno': order_id})

        def positions(self):
            result = self.request('/PositionBook', {'actid': self.userid})
            if isinstance(result, dict) and result.get('emsg', '').lower().startswith('no data'):
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
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--lots', type=int, default=1, help='Number of option lots (default 1); lot size comes from broker')
    parser.add_argument('--nifty-token', default='26000', help='NSE NIFTY index token; verify with broker')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true', help='Read-only candle/quote/option diagnostics; no orders or state changes')
    parser.add_argument('--replay', metavar='YYYY-MM-DD', help='Replay historical one-minute candles; simulated fills only')
    args = parser.parse_args()
    if args.replay and (args.live or args.check):
        parser.error('--replay is historical simulation; cannot combine with --live or --check')
    if args.check and args.live:
        parser.error('--check cannot be combined with --live')
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
    api = create_api()
    api.login(**credentials)  # One authenticated session shared with strategy files.
    import wt_cross
    if args.replay:
        wt_cross.replay_day(api, args)
    elif args.check:
        wt_cross.check_connection(api, args)
    else:
        wt_cross.run(api, args)


if __name__ == '__main__':
    main()
