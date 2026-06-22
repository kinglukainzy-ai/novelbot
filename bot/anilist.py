"""
anilist.py - thin client for AniList's free public GraphQL API.
No API key needed. Used for anime search + next-episode airing info.
"""
import requests

ANILIST_URL = "https://graphql.anilist.co"
TIMEOUT = 15


def search_anime(title: str, limit: int = 5):
    """Returns a list of candidate matches: [{id, title, status, next_episode, airing_at}]"""
    query = """
    query ($search: String, $perPage: Int) {
      Page(perPage: $perPage) {
        media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
          id
          title { romaji english }
          status
          nextAiringEpisode { episode airingAt }
        }
      }
    }
    """
    resp = requests.post(
        ANILIST_URL,
        json={"query": query, "variables": {"search": title, "perPage": limit}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for m in data.get("data", {}).get("Page", {}).get("media", []):
        name = m["title"].get("english") or m["title"].get("romaji")
        next_ep = m.get("nextAiringEpisode")
        results.append({
            "id": m["id"],
            "title": name,
            "status": m["status"],
            "next_episode": next_ep["episode"] if next_ep else None,
            "airing_at": next_ep["airingAt"] if next_ep else None,
        })
    return results


def get_anime_state(anilist_id: int):
    """
    Returns a snapshot string describing the current state, e.g.
    'episode 12 aired' or 'next episode 13 airing at <timestamp>'.
    Used to detect when a new episode has aired since last check.
    """
    query = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        title { romaji english }
        status
        episodes
        nextAiringEpisode { episode airingAt }
      }
    }
    """
    resp = requests.post(
        ANILIST_URL, json={"query": query, "variables": {"id": anilist_id}}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    m = resp.json()["data"]["Media"]
    next_ep = m.get("nextAiringEpisode")
    if next_ep:
        # We are between episodes - the "latest aired" episode is next.episode - 1
        latest_aired = next_ep["episode"] - 1
    else:
        # Show finished airing, or hasn't started
        latest_aired = m.get("episodes") or 0

    name = m["title"].get("english") or m["title"].get("romaji")
    snapshot = f"episode {latest_aired} aired"
    return {
        "title": name,
        "status": m["status"],
        "snapshot": snapshot,
        "latest_aired": latest_aired,
        "next_episode": next_ep["episode"] if next_ep else None,
        "next_airing_at": next_ep["airingAt"] if next_ep else None,
    }
