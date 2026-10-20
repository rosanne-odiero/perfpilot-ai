import httpx


def fetch_profile(user_id: int) -> dict:
    client = httpx.Client(timeout=5)
    response = client.get(f"https://profiles.invalid/{user_id}")
    client.close()
    return response.json()
