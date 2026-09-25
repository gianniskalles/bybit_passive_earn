#!/usr/bin/env python3
"""
Bybit Earn API client for yield rotation strategy.
Handles HMAC signing, health checks, data fetching, and position management.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import urllib.parse

import requests

import settings

# Configuration
BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"
TIMEOUT = 30

class BybitEarnTool:
    def __init__(self, api_key: str = None, api_secret: str = None):
        env = settings.load_env()
        self.api_key = api_key or env.get('BYBIT_API_KEY')
        self.api_secret = api_secret or env.get('BYBIT_API_SECRET')
        
        if not self.api_key or not self.api_secret:
            print("Warning: API credentials not set. Public endpoints will work.")
        
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'X-BAPI-RECV-WINDOW': RECV_WINDOW
        })
    
    def _generate_signature(self, params: str, timestamp: str) -> str:
        """Generate HMAC-SHA256 signature for Bybit API."""
        param_str = f"{timestamp}{self.api_key}{RECV_WINDOW}{params}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Dict:
        """Make HTTP request to Bybit API."""
        if params is None:
            params = {}
        
        # Prepare URL and query string
        url = f"{BASE_URL}{endpoint}"
        query_string = ""
        
        if method.upper() == 'GET' and params:
            query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
            if query_string:
                url += f"?{query_string}"
        
        # Prepare body for POST/PUT
        body = ""
        if method.upper() in ['POST', 'PUT']:
            body = json.dumps(params)
        
        # Add signature if required
        headers = {}
        if signed and self.api_key and self.api_secret:
            timestamp = str(int(time.time() * 1000))
            param_for_sign = query_string if method.upper() == 'GET' else body
            signature = self._generate_signature(param_for_sign, timestamp)
            
            headers.update({
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-SIGN': signature
            })
        
        # Make request
        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                data=body if method.upper() in ['POST', 'PUT'] else None,
                timeout=TIMEOUT
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"API request failed: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            raise
    
    def health(self) -> Dict:
        """Check API connectivity and credential validity."""
        try:
            # Test public endpoint first
            server_time = self._request('GET', '/v5/market/time')
            if server_time.get('retCode') != 0:
                return {
                    'status': 'error',
                    'message': f"Public API failed: {server_time.get('retMsg')}",
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                }
            
            # Test signed endpoint if credentials available
            if self.api_key and self.api_secret:
                account_info = self._request('GET', '/v5/account/info', signed=True)
                if account_info.get('retCode') == 0:
                    return {
                        'status': 'ok',
                        'message': 'API connection and credentials valid',
                        'server_time': server_time.get('result', {}),
                        'account_type': account_info.get('result', {}).get('accountType'),
                        'timestamp': datetime.utcnow().isoformat() + 'Z'
                    }
                else:
                    return {
                        'status': 'error',
                        'message': f"Signed API failed: {account_info.get('retMsg')}",
                        'timestamp': datetime.utcnow().isoformat() + 'Z'
                    }
            else:
                return {
                    'status': 'warning',
                    'message': 'Public API OK, no credentials provided',
                    'server_time': server_time.get('result', {}),
                    'timestamp': datetime.utcnow().isoformat() + 'Z'
                }
        except Exception as e:
            return {
                'status': 'error',
                'message': f"Health check failed: {str(e)}",
                'timestamp': datetime.utcnow().isoformat() + 'Z'
            }
    
    def get_earn_products(self) -> List[Dict]:
        """Fetch all available Earn products (FlexibleSaving category)."""
        params = {'category': 'FlexibleSaving'}
        try:
            result = self._request('GET', '/v5/earn/product', params=params)
            if result.get('retCode') == 0:
                return result.get('result', {}).get('list', [])
            else:
                print(f"Failed to fetch earn products: {result.get('retMsg')}")
                return []
        except Exception as e:
            print(f"Error fetching earn products: {e}")
            return []
    
    def get_earn_apr_history(self, coin: str = None, product_id: str = None,
                            category: str = "FlexibleSaving") -> List[Dict]:
        """Fetch APR history for Earn products (requires category + productId)."""
        params = {'category': category}
        if product_id:
            params['productId'] = product_id
        elif coin:
            # Look up the productId for this coin (first FlexibleSaving product)
            products = [p for p in self.get_earn_products() if p.get('coin') == coin]
            if not products:
                print(f"No FlexibleSaving product found for {coin}")
                return []
            params['productId'] = products[0]['productId']

        try:
            result = self._request('GET', '/v5/earn/apr-history', params=params)
            if result.get('retCode') == 0:
                return result.get('result', {}).get('list', [])
            else:
                print(f"Failed to fetch APR history: {result.get('retMsg')}")
                return []
        except Exception as e:
            print(f"Error fetching APR history: {e}")
            return []
    
    def get_earn_positions(self, coin: str = None) -> List[Dict]:
        """Fetch current Earn positions."""
        params = {'category': 'FlexibleSaving'}
        if coin:
            params['coin'] = coin

        try:
            result = self._request('GET', '/v5/earn/position', params=params, signed=True)
            if result.get('retCode') == 0:
                return result.get('result', {}).get('positionList', [])
            else:
                print(f"Failed to fetch earn positions: {result.get('retMsg')}")
                return []
        except Exception as e:
            print(f"Error fetching earn positions: {e}")
            return []
    
    def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict:
        """Fetch wallet balance."""
        params = {'accountType': account_type}
        try:
            result = self._request('GET', '/v5/account/wallet-balance', params=params, signed=True)
            if result.get('retCode') == 0:
                return result.get('result', {})
            else:
                print(f"Failed to fetch wallet balance: {result.get('retMsg')}")
                return {}
        except Exception as e:
            print(f"Error fetching wallet balance: {e}")
            return {}
    
    def subscribe_earn_product(self, product_id: str, amount: str) -> Dict:
        """Subscribe to an Earn product."""
        params = {
            'productId': product_id,
            'amount': amount
        }
        try:
            result = self._request('POST', '/v5/earn/subscribe', params=params, signed=True)
            return result
        except Exception as e:
            print(f"Error subscribing to earn product: {e}")
            raise
    
    def redeem_earn_product(self, product_id: str, amount: str) -> Dict:
        """Redeem from an Earn product."""
        params = {
            'productId': product_id,
            'amount': amount
        }
        try:
            result = self._request('POST', '/v5/earn/redeem', params=params, signed=True)
            return result
        except Exception as e:
            print(f"Error redeeming earn product: {e}")
            raise
    
    def backfill_apr_history(self, days: int = 180, coin: str = None) -> Dict:
        """Backfill APR history for analysis."""
        print(f"Backfilling {days} days of APR history for {coin or 'all coins'}...")

        products = self.get_earn_products()
        if not products:
            print("No products found")
            return {}

        history_data = {}
        target_coins = [coin] if coin else list(set(p.get('coin') for p in products if p.get('coin')))

        for c in target_coins:
            print(f"Fetching APR history for {c}...")
            apr_history = self.get_earn_apr_history(coin=c)
            if apr_history:
                history_data[c] = apr_history
                print(f"  OK: {len(apr_history)} records")
            else:
                print(f"  No data for {c}")

        return history_data

    def scan_yield_opportunities(self, min_apr_edge: float = 0.009, max_per_product: float = 5.0,
                                 coin_whitelist: List[str] = None) -> List[Dict]:
        """Scan for yield opportunities based on strategy parameters.
        Filters by coin_whitelist to avoid querying APR history for hundreds of products.
        """
        if coin_whitelist is None:
            coin_whitelist = ['USDT']

        print(f"Scanning for yield opportunities (whitelist: {coin_whitelist})...")

        products = self.get_earn_products()
        if not products:
            print("No products found")
            return []

        opportunities = []

        for product in products:
            coin = product.get('coin')
            if coin_whitelist and coin not in coin_whitelist:
                continue

            product_id = product.get('productId')
            estimate_apr_str = product.get('estimateApr', '0%')

            # Parse APR (remove % and convert to decimal)
            try:
                estimate_apr = float(estimate_apr_str.rstrip('%')) / 100
            except ValueError:
                estimate_apr = 0.0

            # Get recent APR history for 24h moving average
            apr_history = self.get_earn_apr_history(coin=coin)
            apr_ma_24h = None

            if apr_history:
                # Take last 24 hourly points for 24h moving average
                recent_aprs = []
                for item in apr_history[-24:]:
                    apr_str = item.get('apr', '0')
                    if apr_str:
                        try:
                            # APR may be "0.8%" (with %) or "0.008" (decimal)
                            if apr_str.endswith('%'):
                                recent_aprs.append(float(apr_str.rstrip('%')) / 100)
                            else:
                                recent_aprs.append(float(apr_str))
                        except ValueError:
                            continue
                if recent_aprs:
                    apr_ma_24h = sum(recent_aprs) / len(recent_aprs)

            # Apply strategy logic
            decision = {
                'product_id': product_id,
                'coin': coin,
                'estimate_apr': estimate_apr,
                'apr_ma_24h': apr_ma_24h,
                'status': product.get('status'),
                'min_stake': float(product.get('minStakeAmount', 0) or 0),
                'max_stake': float(product.get('maxStakeAmount', 0) or 0),
                'remaining_pool': float(product.get('remainingPoolAmount', 0) or 0),
                'redeem_processing_minute': product.get('redeemProcessingMinute', 0),
                'has_tiered_apr': product.get('hasTieredApr', False),
            }

            # Simple opportunity scoring
            if apr_ma_24h is not None and estimate_apr > apr_ma_24h + min_apr_edge:
                decision['signal'] = 'BUY'
                decision['score'] = estimate_apr - apr_ma_24h
            elif estimate_apr > 0:  # Positive APR beats idle (0%)
                decision['signal'] = 'HOLD'
                decision['score'] = estimate_apr
            else:
                decision['signal'] = 'AVOID'
                decision['score'] = 0

            opportunities.append(decision)

        # Sort by score descending
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        return opportunities


def main():
    """Command-line interface for the Bybit Earn tool."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Bybit Earn API client for yield rotation')
    parser.add_argument('--health', action='store_true', help='Check API health')
    parser.add_argument('--backfill', type=int, metavar='DAYS', help='Backfill APR history for N days')
    parser.add_argument('--scan', action='store_true', help='Scan for yield opportunities')
    parser.add_argument('--products', action='store_true', help='List all Earn products')
    parser.add_argument('--positions', action='store_true', help='Show current Earn positions')
    parser.add_argument('--balance', action='store_true', help='Show wallet balance')
    parser.add_argument('--coin', type=str, help='Filter by coin (e.g., USDT)')
    parser.add_argument('--product-id', type=str, help='Filter by product ID')
    parser.add_argument('--amount', type=str, help='Amount for subscribe/redeem operations')
    parser.add_argument('--subscribe', type=str, metavar='PRODUCT_ID', help='Subscribe to product')
    parser.add_argument('--redeem', type=str, metavar='PRODUCT_ID', help='Redeem from product')
    parser.add_argument('--min-apr-edge', type=float, default=0.009, help='Minimum APR edge for signals')
    parser.add_argument('--max-per-product', type=float, default=5.0, help='Maximum USD per product')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    
    args = parser.parse_args()
    
    # Initialize tool
    tool = BybitEarnTool()
    
    # Handle commands
    if args.health:
        result = tool.health()
        print(json.dumps(result, indent=2))
        return
    
    if args.backfill:
        history = tool.backfill_apr_history(days=args.backfill, coin=args.coin)
        print(json.dumps(history, indent=2, default=str))
        return
    
    if args.scan:
        whitelist = [args.coin] if args.coin else ['USDT']
        opportunities = tool.scan_yield_opportunities(
            min_apr_edge=args.min_apr_edge,
            max_per_product=args.max_per_product,
            coin_whitelist=whitelist
        )
        print(json.dumps(opportunities, indent=2))
        return
    
    if args.products:
        products = tool.get_earn_products()
        print(json.dumps(products, indent=2))
        return
    
    if args.positions:
        positions = tool.get_earn_positions(coin=args.coin)
        print(json.dumps(positions, indent=2))
        return
    
    if args.balance:
        # Bybit API only supports UNIFIED accountType for /v5/account/wallet-balance
        # (including Earn products, which draw from the Unified trading account)
        balance = tool.get_wallet_balance(account_type="UNIFIED")
        if balance.get('list'):
            print("=== UNIFIED account (Bybit only supports this type) ===")
            for coin_data in balance['list'][0].get('coin', []):
                if coin_data.get('walletBalance', '0') != '0' or coin_data.get('equity', '0') != '0':
                    print(f"  {coin_data['coin']}: equity={coin_data.get('equity')}, wallet={coin_data.get('walletBalance')}")
        else:
            print("No balances found")
        return
    
    if args.subscribe:
        if not args.amount:
            print("Error: --amount required for subscribe")
            sys.exit(1)
        result = tool.subscribe_earn_product(args.subscribe, args.amount)
        print(json.dumps(result, indent=2))
        return
    
    if args.redeem:
        if not args.amount:
            print("Error: --amount required for redeem")
            sys.exit(1)
        result = tool.redeem_earn_product(args.redeem, args.amount)
        print(json.dumps(result, indent=2))
        return
    
    # Default: show help
    parser.print_help()


if __name__ == '__main__':
    main()