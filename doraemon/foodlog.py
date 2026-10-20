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


# --- photos: a nutrition label or the food itself -----------------------------------------------------

_NUM = {"type": "number"}
PHOTO_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["label", "food", "neither"]},
    "product": {"type": "string"}, "basis": {"type": "string", "enum": ["100 g", "100 ml", "serving", "pack"]},
    "serving_size": {"type": "string"}, "pack_size": {"type": "string"},
    "kcal": _NUM, "kj": _NUM, "protein": _NUM, "carbs": _NUM, "fat": _NUM, "sugar": _NUM,
    "foods": SCHEMA["properties"]["foods"]},
    "required": ["kind", "product", "basis", "serving_size", "pack_size", "kcal", "kj", "protein", "carbs", "fat",
                 "sugar", "foods"]}

PHOTO_PROMPT = """You read photos people in Singapore send to log what they ate or drank.
First decide what the photo shows:
- "label": a nutrition information panel (a table of energy, protein, fat, carbohydrate...). Copy its numbers exactly, don't estimate. If it has a "per 100 g" or "per 100 ml" column, read that column and set basis to "100 g" or "100 ml"; otherwise read the "per serving" column (basis "serving"), or "per pack" (basis "pack"). kcal is the energy in kcal; if the label only gives kJ, put that in kj and 0 in kcal. carbs is total carbohydrate, sugar is total sugars. Copy the serving size and the package size as printed (e.g. "250 ml", "4 x 30 g"), or "" if not shown. Put the brand and product name in product if you can see it. Leave foods empty.
- "food": a meal, a dish, a snack or a drink (not a label). List each food or drink you can see with a short name, the portion you see (e.g. "1 plate", "2 pieces", "1 cup"), and your estimate for that portion of kcal, protein, carbs, fat and sugar in grams, using typical values for Singapore food. Take what they said into account (e.g. "half", "no rice"). If it's a branded packaged product, put the brand and product in that food's product. Set the label fields to 0 or "".
- "neither": anything else.
Numbers only, no ranges. Answer with JSON."""

_COUNT = r"(\d+(?:\.\d+)?|an?|one|two|three|four|half(?: an?| the)?|a half|a quarter|quarter(?: of)?(?: an?| the)?)"
_SERVINGS = re.compile(r"\b" + _COUNT + r"\s+(?:of\s+(?:an?|the)\s+)?(?:servings?|portions?|glass(?:es)?|cups?|bowls?|scoops?)\b", re.I)
_PACKS = re.compile(r"\b" + _COUNT + r"\s+(?:of\s+(?:an?|the)\s+)?(?:packs?|box(?:es)?|bags?|cartons?|tubs?|tins?)\b", re.I)
# one of a multipack's items ("2 bars" of "5 x 40 g"); on a single pack, the whole pack
_UNITS = re.compile(r"\b" + _COUNT + r"\s+(?:of\s+(?:an?|the)\s+)?(?:bars?|packets?|sachets?|bottles?|cans?|pieces?|"
                    r"sticks?|biscuits?|cookies?)\b", re.I)
_WHOLE = re.compile(r"\b(?:whole|entire|all of it|the lot|finished it)\b", re.I)
_HALF = re.compile(r"\bhalf\b", re.I)
_MULTI = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(ml|l|g|kg)\b", re.I)


def _count(word: str) -> float:
    word = word.lower().strip()
    if word.startswith("half") or word == "a half":
        return 0.5
    if "quarter" in word:
        return 0.25
    return {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4}.get(word) or float(word)


def parse_amount(text: str | None) -> dict | None:
    """How much of a packaged food you had, from what you said: {"size": (250, "ml")}, {"servings": 2} or
    {"packs": 0.5}; None if you didn't say."""
    if not (text or "").strip():
        return None
    if size := amount_of(text):
        return {"size": size}
    if m := _SERVINGS.search(text):
        return {"servings": _count(m[1])}
    if m := _PACKS.search(text):
        return {"packs": _count(m[1])}
    if m := _UNITS.search(text):
        return {"units": _count(m[1])}
    if _WHOLE.search(text):
        return {"packs": 1.0}
    if _HALF.search(text):
        return {"packs": 0.5}
    return None


def _pack_amount(text: str) -> tuple[tuple[float, str] | None, tuple[float, str] | None]:
    """(the pack, each item in it): '1L' -> ((1000, 'ml'), None); '4 x 30 g' -> ((120, 'g'), (30, 'g'))."""
    if m := _MULTI.search(text or ""):
        each = amount_of(f"{m[2]} {m[3]}")
        return ((int(m[1]) * each[0], each[1]), each) if each else (None, None)
    return amount_of(text), None


def _times(n: float, one: str) -> str:
    """'1 serving', '2 servings', 'half a serving', '1.5 servings'."""
    if n == 0.5:
        return f"half a {one}"
    return f"{n:g} {one}" + ("" if n == 1 else "s")


def label_unit(reading: dict) -> str | None:
    """'g' or 'ml': what the label measures in, if it says."""
    basis = reading.get("basis") or ""
    if basis.startswith("100"):
        return basis.split()[1]
    found = amount_of(reading.get("serving_size")) or _pack_amount(reading.get("pack_size") or "")[0]
    return found[1] if found else None


def label_food(reading: dict, said: str | None = None) -> dict | None:
    """The food a nutrition label describes, worked out for how much you had (what you said; else one serving, else
    the pack, else 100 g or ml). The model only copies the label: the app does the sums. None if it read no numbers."""
    per = {m: max(0.0, float(reading.get(m) or 0)) for m in MACROS}
    if not per["kcal"] and float(reading.get("kj") or 0) > 0:
        per["kcal"] = float(reading["kj"]) / 4.184
    if not per["kcal"] and not per["protein"]:
        return None
    basis = reading.get("basis") or "serving"
    serving, (pack, each) = amount_of(reading.get("serving_size")), _pack_amount(reading.get("pack_size") or "")
    want = parse_amount(said) or {}
    if "units" in want:  # "2 bars": that many of a multipack's items, or of the pack itself
        n = want.pop("units")
        want = {"size": (n * each[0], each[1])} if each else {"packs": n}
    if "size" in want and label_unit(reading) not in (None, want["size"][1]):
        want = {}  # "330 ml" of a label in grams: no telling, so the usual amount
    if basis in ("100 g", "100 ml"):
        unit = basis.split()[1]
        if "size" in want:
            size, portion = want["size"][0], None
        elif "servings" in want and serving:
            size, portion = want["servings"] * serving[0], _times(want["servings"], "serving")
        elif "packs" in want and pack:
            size, portion = want["packs"] * pack[0], _times(want["packs"], "pack")
        elif serving:
            size, portion = serving[0], "1 serving"
        elif pack:
            size, portion = pack[0], "1 pack"
        else:
            size, portion = 100.0, None
        factor = size / 100
        portion = f"{portion} ({size:g} {unit})" if portion else f"{size:g} {unit}"
    else:  # the label's numbers are per serving or per pack
        one, one_size = ("serving", serving) if basis == "serving" else ("pack", pack)
        other, others = (pack, "packs") if basis == "serving" else (serving, "servings")
        if f"{one}s" in want:
            factor = want[f"{one}s"]
        elif "size" in want and one_size:
            factor = want["size"][0] / one_size[0]
        elif others in want and one_size and other:
            factor = want[others] * other[0] / one_size[0]
        else:
            factor = 1.0
        factor = round(factor, 2)
        portion = _times(factor, one) + (f" ({round(factor * one_size[0]):g} {one_size[1]})" if one_size else "")
    name = str(reading.get("product") or "").strip()[:80] or "Packaged food"
    return {"name": name, "portion": portion[:40], **{m: r1(per[m] * factor) for m in MACROS},
            "source": "label", "note": "your label photo"}


def read_photo(image: bytes, said: str | None, fit: Fitness, db: Database, backend, fetch=None) -> dict:
    """What a photo shows: {"kind": "label", "foods": [one], "reading": the label as read} for a nutrition label,
    {"kind": "food", "foods": [...]} for a meal or drink (estimates, then your numbers and Open Food Facts where they
    know better), {"kind": "unreadable"} for a label whose numbers it couldn't read, or {"kind": "neither"}."""
    user = "What's in this photo?" + (f' They said: "{said.strip()[:200]}"' if (said or "").strip() else "")
    answer = json.loads(backend.complete_json(PHOTO_PROMPT, user, PHOTO_SCHEMA, images=[image]))
    if answer.get("kind") == "label":
        reading = {k: answer.get(k) for k in ("product", "basis", "serving_size", "pack_size", "kj", *MACROS)}
        food = label_food(reading, said)
        return {"kind": "label", "foods": [food], "reading": reading} if food else {"kind": "unreadable"}
    if answer.get("kind") == "food":
        foods = [{"name": str(f["name"]).strip()[:80], "portion": str(f.get("portion") or "").strip()[:40],
                  **{m: max(0.0, r1(f.get(m))) for m in MACROS}, "product": str(f.get("product") or "").strip(),
                  "source": "estimate"} for f in answer.get("foods") or [] if str(f.get("name") or "").strip()]
        return {"kind": "food", "foods": resolve(foods, fit, db, fetch)} if foods else {"kind": "neither"}
    return {"kind": "neither"}


_FILLER = re.compile(r"\b(?:i|had|drank|ate|have|actually|only|just|about|around|roughly|it|was|make|of|please|"
                     r"no|oh|sorry|ok|okay)\b|[,!]|\.(?!\d)", re.I)


def amount_reply(text: str) -> dict | None:
    """A message that only says how much ("330 ml", "2 servings", "actually half the pack"), answering a label just
    read: the amount, as parse_amount gives it; None for anything else."""
    rest = " ".join(_FILLER.sub(" ", text).split())
    if not rest or len(rest.split()) > 4:
        return None
    found = parse_amount(rest)
    if found is None:
        return None
    # what's left must be the amount itself, not "2 servings of rice"
    spent = re.sub(r"\b(?:\d+(?:\.\d+)?\s*(?:ml|l|litres?|liters?|g|kg)|the|an?|whole|entire|half|lot|all|thing|quarter|servings?|portions?|packs?|box(?:es)?|bags?|"
                   r"cartons?|tubs?|tins?|bars?|packets?|sachets?|bottles?|cans?|pieces?|sticks?|cups?|biscuits?|"
                   r"cookies?|glass(?:es)?|bowls?|scoops?|one|two|three|four|ml|l|litres?|liters?|g|kg|\d+(?:\.\d+)?)\b",
                   " ", rest, flags=re.I)
    return found if not spent.strip() else None
