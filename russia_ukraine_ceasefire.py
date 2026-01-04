#!/usr/bin/env python3
"""
Polymarket Russia-Ukraine Ceasefire Monitor

Monitors the "Russia x Ukraine ceasefire by January 31, 2026?" market on Polymarket
and sends a WhatsApp notification via Twilio if "Yes" odds exceed 20%.

Uses the Gamma API (https://gamma-api.polymarket.com) which is the fastest option
for fetching market data - it's a lightweight REST API optimized for market info.
"""

import os
import sys
import requests
from twilio.rest import Client

# =============================================================================
# CONFIGURATION
# =============================================================================

# Polymarket Gamma API - fastest option for market data (simple REST, no auth needed)
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Market search parameters
MARKET_TITLE = "Russia x Ukraine ceasefire by January 31, 2026?"
ODDS_THRESHOLD = 0.20  # 20%

# Twilio credentials (set via environment variables for security)
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")  # Twilio sandbox default

# Your WhatsApp number (E.164 format with country code)
TARGET_WHATSAPP_NUMBER = "whatsapp:+14128556538"


# =============================================================================
# POLYMARKET API FUNCTIONS
# =============================================================================

def search_market_by_title(title_query: str) -> dict | None:
    """
    Search for a market by title using the Gamma API.
    
    The Gamma API supports searching markets with query parameters.
    This is faster than fetching all markets and filtering client-side.
    
    Args:
        title_query: The market title to search for
        
    Returns:
        Market data dict if found, None otherwise
    """
    url = f"{GAMMA_API_BASE}/markets"
    
    # The Gamma API supports text search via the 'title' parameter
    params = {
        "title": title_query,
        "closed": "false",  # Only active markets
        "limit": 10
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        markets = response.json()
        
        # Find exact or closest match
        for market in markets:
            if market.get("question", "").strip() == title_query or \
               market.get("title", "").strip() == title_query:
                return market
        
        # If no exact match, return first result if it contains key terms
        if markets and ("ceasefire" in markets[0].get("question", "").lower() or 
                       "ceasefire" in markets[0].get("title", "").lower()):
            return markets[0]
            
        return None
        
    except requests.RequestException as e:
        print(f"Error fetching market data: {e}")
        return None


def get_market_by_slug(slug: str) -> dict | None:
    """
    Get a specific market by its slug (URL identifier).
    
    This is the fastest method if you know the market's slug.
    
    Args:
        slug: The market's URL slug
        
    Returns:
        Market data dict if found, None otherwise
    """
    url = f"{GAMMA_API_BASE}/markets/{slug}"
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Error fetching market by slug: {e}")
        return None


def get_yes_odds(market: dict) -> float | None:
    """
    Extract the "Yes" outcome odds/probability from market data.
    
    The Gamma API returns odds in different formats depending on market type.
    For binary markets, it's typically in 'outcomePrices' or 'outcomes'.
    
    Args:
        market: Market data dictionary
        
    Returns:
        Yes odds as a float (0.0 to 1.0), or None if not found
    """
    # Method 1: Check 'outcomePrices' (most common format)
    # Format: "[\"0.85\", \"0.15\"]" where first is Yes, second is No
    if "outcomePrices" in market and market["outcomePrices"]:
        try:
            import json
            prices = json.loads(market["outcomePrices"])
            if prices and len(prices) >= 1:
                return float(prices[0])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    
    # Method 2: Check direct 'outcomes' array
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, list):
        for outcome in outcomes:
            if isinstance(outcome, dict):
                name = outcome.get("name", "").lower()
                if name == "yes":
                    if "price" in outcome:
                        return float(outcome["price"])
                    if "probability" in outcome:
                        return float(outcome["probability"])
    
    # Method 3: For binary markets, check 'price' directly
    if "price" in market:
        return float(market["price"])
    
    # Method 4: Check 'bestBid' or 'bestAsk' for order book based pricing
    if "bestBid" in market:
        return float(market["bestBid"])
    
    return None


# =============================================================================
# WHATSAPP NOTIFICATION FUNCTIONS
# =============================================================================

def send_whatsapp_notification(message: str) -> bool:
    """
    Send a WhatsApp message via Twilio.
    
    Args:
        message: The message text to send
        
    Returns:
        True if sent successfully, False otherwise
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        print("ERROR: Twilio credentials not set!")
        print("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN environment variables.")
        return False
    
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        msg = client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_FROM,
            to=TARGET_WHATSAPP_NUMBER
        )
        
        print(f"WhatsApp notification sent! Message SID: {msg.sid}")
        return True
        
    except Exception as e:
        print(f"Failed to send WhatsApp notification: {e}")
        return False


# =============================================================================
# MAIN LOGIC
# =============================================================================

def check_and_notify() -> dict:
    """
    Main function to check market odds and send notification if threshold exceeded.
    
    Returns:
        dict with status information
    """
    result = {
        "market_found": False,
        "yes_odds": None,
        "threshold_exceeded": False,
        "notification_sent": False,
        "error": None
    }
    
    print(f"Searching for market: {MARKET_TITLE}")
    
    # Search for the market
    market = search_market_by_title(MARKET_TITLE)
    
    if not market:
        # Try alternate search terms
        print("Trying alternate search...")
        market = search_market_by_title("Russia Ukraine ceasefire January 2026")
    
    if not market:
        result["error"] = "Market not found"
        print(f"ERROR: {result['error']}")
        return result
    
    result["market_found"] = True
    market_question = market.get("question", market.get("title", "Unknown"))
    print(f"Found market: {market_question}")
    
    # Get Yes odds
    yes_odds = get_yes_odds(market)
    
    if yes_odds is None:
        result["error"] = "Could not extract Yes odds from market data"
        print(f"ERROR: {result['error']}")
        print(f"Market data keys: {list(market.keys())}")
        return result
    
    result["yes_odds"] = yes_odds
    yes_percentage = yes_odds * 100
    threshold_percentage = ODDS_THRESHOLD * 100
    
    print(f"Current 'Yes' odds: {yes_percentage:.2f}%")
    print(f"Threshold: {threshold_percentage:.2f}%")
    
    # Check if threshold exceeded
    if yes_odds > ODDS_THRESHOLD:
        result["threshold_exceeded"] = True
        print(f"ALERT: Yes odds ({yes_percentage:.2f}%) exceed threshold ({threshold_percentage:.2f}%)!")
        
        # Send notification
        message = (
            f"🚨 POLYMARKET ALERT 🚨\n\n"
            f"Market: {market_question}\n\n"
            f"'Yes' odds are now at {yes_percentage:.2f}%\n"
            f"(Threshold: {threshold_percentage:.2f}%)\n\n"
            f"Check it out: https://polymarket.com"
        )
        
        result["notification_sent"] = send_whatsapp_notification(message)
    else:
        print(f"No alert needed. Yes odds ({yes_percentage:.2f}%) below threshold ({threshold_percentage:.2f}%).")
    
    return result


def main():
    """Entry point for the script."""
    print("=" * 60)
    print("Polymarket Russia-Ukraine Ceasefire Monitor")
    print("=" * 60)
    print()
    
    result = check_and_notify()
    
    print()
    print("-" * 60)
    print("Summary:")
    print(f"  Market found: {result['market_found']}")
    print(f"  Yes odds: {result['yes_odds'] * 100:.2f}%" if result['yes_odds'] else "  Yes odds: N/A")
    print(f"  Threshold exceeded: {result['threshold_exceeded']}")
    print(f"  Notification sent: {result['notification_sent']}")
    if result['error']:
        print(f"  Error: {result['error']}")
    print("-" * 60)
    
    # Exit with error code if there was an issue
    if result['error']:
        sys.exit(1)
    
    return result


if __name__ == "__main__":
    main()

