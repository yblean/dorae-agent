"""What you ate, as foods with calories and macros, for Dorae-3 to log.

"Chicken rice and kopi siew dai" -> two foods, each with a portion, kcal, protein, carbs, fat and sugar:
1. A food you've logged before comes back with your own numbers (doraemon.fitness: the library).
2. A packaged product ("NutriSoy no sugar 250ml") is looked up in Open Food Facts (free, no key; only the
   product's name is sent), Singapore products first, and worked out for your portion.
3. Anything else is the local model's estimate, for a typical Singapore portion.
Nothing is logged until you press Log, and what you correct is what the library remembers.
"""
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from doraemon.db import Database
from doraemon.fitness import MACROS, Fitness, key_of, r1

log = logging.getLogger(__name__)

SEARCH = "https://search.openfoodfacts.org/search"
FIELDS = "product_name,brands,code,nutriments,countries_tags,quantity,product_quantity,product_quantity_unit,serving_size"

SCHEMA = {"type": "object", "properties": {"foods": {"type": "array", "items": {"type": "object", "properties": {
    "name": {"type": "string", "description": "the food, short, e.g. Chicken rice"},
    "portion": {"type": "string", "description": "how much, e.g. '1 plate', '2 pieces', '250 ml'"},
    "kcal": {"type": "number"}, "protein": {"type": "number"}, "carbs": {"type": "number"},
    "fat": {"type": "number"}, "sugar": {"type": "number"},
    "product": {"type": "string", "description": "brand and product name if it's a packaged product, else ''"}},
    "required": ["name", "portion", "kcal", "protein", "carbs", "fat", "sugar", "product"]}}}, "required": ["foods"]}

PROMPT = """You estimate the nutrition of what someone in Singapore ate, from their own words.
Split the message into separate foods and drinks. For each give a short name, the portion (theirs if they said one, otherwise a typical single serving in Singapore, e.g. a hawker plate or a cup), and for that portion: kcal, protein, carbs, fat and sugar in grams.
Use typical values for Singapore hawker food and drinks (kopi/teh with condensed milk, siew dai = less sweet, kosong = no sugar, peng = iced).
If it's a branded packaged product (e.g. NutriSoy, Yakult, Pokka), put the brand and product in "product".
Don't add foods they didn't mention. Numbers only, no ranges. Answer with JSON."""

_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|l|litres?|liters?|g|kg)\b", re.I)


def amount_of(text: str | None) -> tuple[float, str] | None:
    """(amount, 'ml' or 'g') from '250ml', '1 carton (250 ml)', '1L', '200 g'; None if it doesn't say."""
    m = _AMOUNT.search(text or "")
    if not m:
        return None
    n, unit = float(m[1]), m[2].lower()
    if unit.startswith("l"):
        return n * 1000, "ml"
    if unit == "kg":
        return n * 1000, "g"
    return n, unit


# --- Open Food Facts ----------------------------------------------------------------------------------

def _score(product: dict, words: set[str]) -> float:
    name = key_of(f"{' '.join(product.get('brands') or []) if isinstance(product.get('brands'), list) else product.get('brands') or ''} "
                  f"{product.get('product_name') or ''}")
    overlap = len(words & set(name.split())) / max(1, len(words))
    local = "en:singapore" in (product.get("countries_tags") or []) or str(product.get("code", "")).startswith("888")
    return overlap + (0.5 if local else 0)


def lookup_product(db: Database, query: str, fetch=None) -> dict | None:
    """The best Open Food Facts match for a product name, per 100 g or ml, or None. Cached in food_facts.
    `fetch(url, params) -> dict`: tests pass a fake."""
    key = key_of(query)
    if not key:
        return None
    cached = db.conn.execute("SELECT found FROM food_facts WHERE query = ?", (key,)).fetchone()
    if cached:
        return json.loads(cached["found"])
    fetch = fetch or _fetch
    try:
        data = fetch(SEARCH, {"q": query, "page_size": 12, "fields": FIELDS})
    except Exception as e:  # offline or the service is down: the model's estimate stands
        log.info("Open Food Facts lookup failed: %s", e)
        return None
    words = set(key.split())
    best, best_score = None, 0.0
    for p in data.get("hits") or data.get("products") or []:
        n = p.get("nutriments") or {}
        if n.get("energy-kcal_100g") is None:
            continue
        score = _score(p, words)
        if score > best_score:
            best, best_score = p, score
    found = None
    if best is not None and best_score >= 0.6:  # most of the words you used are in its name
        n = best["nutriments"]
        brands = best.get("brands")
        brand = ", ".join(brands) if isinstance(brands, list) else (brands or "")
        title = best.get("product_name") or ""
        if brand and not key_of(title).startswith(key_of(brand)):  # "Nutrisoy No sugar", not "Nutrisoy Nutrisoy No sugar"
            title = f"{brand} {title}"
        found = {"name": title.strip(), "code": best.get("code"),
                 "per100": {"kcal": n.get("energy-kcal_100g"), "protein": n.get("proteins_100g"),
                            "carbs": n.get("carbohydrates_100g"), "fat": n.get("fat_100g"), "sugar": n.get("sugars_100g")},
                 "pack": amount_of(str(best.get("quantity") or "")), "serving": amount_of(str(best.get("serving_size") or ""))}
    db.conn.execute("INSERT OR REPLACE INTO food_facts (query, found, fetched_at) VALUES (?, ?, ?)",
                    (key, json.dumps(found), datetime.now(timezone.utc).isoformat()))
    db.conn.commit()
    return found


def _fetch(url: str, params: dict) -> dict:
    resp = httpx.get(url, params=params, timeout=10, headers={"User-Agent": "Doraemon/0.1 (personal food log)"})
    resp.raise_for_status()
    return resp.json()


# --- the whole message --------------------------------------------------------------------------------

def estimate(text: str, backend) -> list[dict]:
    """The model's reading of the message: foods with a portion and numbers for it."""
    answer = json.loads(backend.complete_json(PROMPT, f"They ate: {text}", SCHEMA))
    foods = []
    for f in answer.get("foods") or []:
        if not str(f.get("name") or "").strip():
            continue
        foods.append({"name": str(f["name"]).strip()[:80], "portion": str(f.get("portion") or "").strip()[:40],
                      **{m: max(0.0, r1(f.get(m))) for m in MACROS}, "product": str(f.get("product") or "").strip(),
                      "source": "estimate"})
    return foods


def resolve(foods: list[dict], fit: Fitness, db: Database, fetch=None) -> list[dict]:
    """Better numbers where there are some: yours from the library, then the product's own from Open Food Facts."""
    out = []
    for f in foods:
        mine = fit.known(f["name"]) or (fit.known(f["product"]) if f.get("product") else None)
        if mine is not None:
            out.append({"name": mine["name"], "portion": mine["portion"] or f["portion"],
                        **{m: mine[m] for m in MACROS}, "source": "yours", "note": "your usual"})
            continue
        product = lookup_product(db, f["product"], fetch) if f.get("product") else None
        size = amount_of(f["portion"]) or (product or {}).get("pack") or (product or {}).get("serving")
        if product and size and all(product["per100"].get(m) is not None for m in ("kcal", "protein")):
            factor = size[0] / 100
            out.append({"name": f["name"], "portion": f["portion"] or f"{size[0]:g} {size[1]}",
                        **{m: r1((product["per100"].get(m) or 0) * factor) for m in MACROS},
                        "source": "database", "note": f"{product['name']} (Open Food Facts)"})
            continue
        out.append({**f, "note": ""})
    return out


def understand(text: str, fit: Fitness, db: Database, backend, fetch=None) -> list[dict]:
    """The foods in a message, best numbers first. "My usual breakfast" (a whole message you've logged before as one
    food) needs no model at all."""
    mine = fit.known(text)
    if mine is not None:
        return [{"name": mine["name"], "portion": mine["portion"], **{m: mine[m] for m in MACROS},
                 "source": "yours", "note": "your usual"}]
    return resolve(estimate(text, backend), fit, db, fetch)
