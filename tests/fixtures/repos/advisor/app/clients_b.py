import requests


def fetch_prices(sku: str) -> dict:
    session = requests.Session()
    response = session.get(f"https://prices.invalid/{sku}")
    session.close()
    return response.json()
