"""Dorae-3: targets, logging food, steps from Apple Health, the 6pm check, its chat and Telegram."""
import dataclasses
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from doraemon import coach, nudges
from doraemon.agents import short_clock
from doraemon.db import Database
from doraemon.fitness import Fitness, import_steps, parse_steps, suggest_targets
from doraemon.foodlog import (amount_of, amount_reply, given_numbers, label_food, lookup_product, resolve, said_count,
                              understand)
from doraemon.jobs import Runtime
from doraemon.router import route
from doraemon.web import create_app

SG = ZoneInfo("Asia/Singapore")


class FakeFood:
    """The model reading food: canned answers by the words in the message."""
    name = "fake"

    def __init__(self, foods=None):
        self.calls = []
        self.foods = foods or [{"name": "Chicken rice", "portion": "1 plate", "kcal": 600, "protein": 25, "carbs": 75,
                                "fat": 20, "sugar": 2, "product": ""},
                               {"name": "Kopi siew dai", "portion": "1 cup", "kcal": 110, "protein": 2, "carbs": 18,
                                "fat": 3, "sugar": 14, "product": ""}]

    def complete_json(self, system, user, schema):
        self.calls.append(user)
        return json.dumps({"foods": self.foods})


class FakeBot:
    chat_id = "42"

    def __init__(self):
        self.sent = []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))


@pytest.fixture
def fit(tmp_path):
    return Fitness(Database(tmp_path / "d.db"), "Asia/Singapore")


@pytest.fixture
def settings(tmp_path, settings):
    return dataclasses.replace(settings, db_path=str(tmp_path / "d.db"), timezone="Asia/Singapore", home_currency="SGD",
                               poll_minutes=0, google_token=str(tmp_path / "t.json"),
                               google_credentials=str(tmp_path / "c.json"), health_folder=str(tmp_path / "health"))


# --- targets ----------------------------------------------------------------------------------------

def test_suggested_targets_follow_the_profile():
    cut = suggest_targets({"weight_kg": 70, "height_cm": 175, "age": 23, "sex": "male", "activity": "light", "goal": "lose"})
    assert cut == {"kcal": 1850, "protein": 140, "steps": 8000}
    keep = suggest_targets({"weight_kg": 58, "height_cm": 162, "age": 25, "sex": "female", "activity": "moderate",
                            "goal": "maintain"})
    assert keep == {"kcal": 2050, "protein": 105, "steps": 10000}


def test_targets_set_in_words():
    assert coach.parse_target("set my protein target to 140g") == {"protein": 140}
    assert coach.parse_target("set calories to 1,900") == {"kcal": 1900}
    assert coach.parse_target("set my daily steps goal to 10000") == {"steps": 10000}
    assert coach.parse_target("set my dining budget to 300") is None


NOW = {"kcal": 2150, "protein": 120, "steps": 8000}


@pytest.mark.parametrize("text, target", [
    ("change my daily calories to 1950", {"kcal": 1950}),
    ("make my calories 1950", {"kcal": 1950}),
    ("my calorie target should be 1950", {"kcal": 1950}),
    ("update calorie goal to 1950kcal", {"kcal": 1950}),
    ("change my calorie intake to 1,950", {"kcal": 1950}),
    ("i want to eat 1950 calories a day", {"kcal": 1950}),
    ("can you change protein to 130g", {"protein": 130}),
    ("set steps to 10k", {"steps": 10000}),
    ("set calories to 1950 and protein to 140", {"kcal": 1950, "protein": 140}),
    ("lower my calories by 100", {"kcal": 2050}),  # from today's 2150
    ("raise protein by 20", {"protein": 140}),
    ("had 300 calories", None),
    ("change protein to 5", None),  # not a sensible target
    ("how can i lower my calories by 200?", None),  # a question, not a change
    ("what should i set my calories to?", None),
    ("ate 2 eggs 140 kcal", None),
])
def test_targets_changed_in_your_own_words(text, target):
    assert coach.parse_target(text, NOW) == target


def test_changing_a_target_in_chat_and_on_telegram(settings):
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    brain.fit.set_targets(**NOW)
    html = client.post("/chat/fitness/ask", data={"q": "change my daily calories to 1950"}).json()["html"]
    assert "Done: 1,950 kcal a day." in html and brain.fit.targets()["kcal"] == 1950
    text, _ = Runtime(settings, messenger=FakeBot()).phone.answer("set protein to 140 and steps to 10k")
    assert text.startswith("✅ Done: 140 g protein and 10,000 steps a day.")
    assert brain.fit.targets() == {"kcal": 1950, "protein": 140, "steps": 10000}


# --- food ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, own, expected", [
    ("had chicken rice and kopi", True, "chicken rice and kopi"),
    ("had a long meeting", False, None),  # on Telegram "had" alone isn't food
    ("ate laksa", False, "laksa"),
    ("lunch: laksa", False, "laksa"),
    ("nutrisoy 250ml for breakfast", False, "nutrisoy 250ml"),
    ("chicken rice and kopi siew dai", True, "chicken rice and kopi siew dai"),  # Dorae-3's chat: a statement is food
    ("chicken rice and kopi siew dai", False, None),
    ("how much protein today?", True, None),
    ("set my protein target to 140", True, None),
    ("i had 3 eggs and 2 bread", False, "3 eggs and 2 bread"),  # "had" is food on Telegram too...
    ("had a protein bar", False, "a protein bar"),
    ("i've had a burger", False, "a burger"),
    ("had to go to work", False, None),  # ...unless it's plainly not
    ("had a call with mom", False, None),
    ("i had a good time", False, None),
    ("can you log chicken rice for me", False, "chicken rice"),
    ("please log 3 eggs", False, "3 eggs"),
    ("pls log my lunch, chicken rice", False, "chicken rice"),
    ("log my breakfast: 2 eggs and toast", False, "2 eggs and toast"),
    ("add chicken rice to my food log", False, "chicken rice"),
    ("track 2 eggs and toast", False, "2 eggs and toast"),
    ("track my protein", False, None),
    ("for lunch i had chicken rice", False, "chicken rice"),
    ("this morning i had oats", False, "oats"),
    ("yesterday i had chicken rice", False, "chicken rice"),
    ("im eating laksa now", False, "laksa"),
    ("just finished a bowl of pho", False, "a bowl of pho"),
    ("dinner - laksa", False, "laksa"),
    ("my lunch was chicken rice", False, "chicken rice"),
    ("had tea and biscuits", False, "tea and biscuits"),  # "tea" is the food here, not the meal
    ("tea and biscuits", True, "tea and biscuits"),
    ("lunch with sam", False, None),
    ("had enough protein today?", False, None),
    ("can you log 2 eggs?", False, "2 eggs"),
])
def test_what_counts_as_food_to_log(text, own, expected):
    assert coach.food_request(text, own) == expected


@pytest.mark.parametrize("text, expected", [
    ("2 eggs, 12g protein", True),
    ("chicken breast 3 pieces 15g protein each", True),
    ("protein bar 200 kcal", True),
    ("calories 1950", False),  # no food named
    ("protein 40g", False),
    ("how much protein in 2 eggs", False),
    ("my protein today 50g", False),
])
def test_a_food_with_your_numbers(text, expected):
    assert coach.food_with_numbers(text) is expected


def test_food_never_changes_a_target():
    assert coach.parse_target("had a 1000 calorie lunch, ended up full", NOW) is None
    assert coach.parse_target("up my protein to 150", NOW) == {"protein": 150}


def test_a_meal_with_only_its_calories(fit):
    nothing = FakeFood()
    nothing.foods = []  # the model finds no food named
    foods = understand("had a 1000 calorie lunch", fit, fit.db, nothing)
    assert [(f["name"], f["kcal"], f["source"]) for f in foods] == [("Lunch (1000 kcal)", 1000, "you")]


def test_a_meal_is_a_draft_until_logged_then_remembered(fit):
    batch = fit.draft([{"name": "Chicken rice", "portion": "1 plate", "kcal": 600, "protein": 25}])
    assert fit.totals(fit.today()).kcal == 0  # a draft doesn't count
    fit.log(batch)
    assert fit.totals(fit.today()).kcal == 600 and fit.known("chicken rice")["protein"] == 25
    row = fit.foods_on(fit.today())[0]
    fit.edit(row["id"], kcal=650, protein=30)  # your correction is what's remembered
    assert fit.known("Chicken Rice")["kcal"] == 650 and fit.totals(fit.today()).protein == 30


def test_your_foods_come_back_with_your_numbers_without_the_model(fit):
    fit.log(fit.draft([{"name": "My usual breakfast", "portion": "", "kcal": 420, "protein": 32}]))
    model = FakeFood()
    foods = understand("my usual breakfast", fit, fit.db, model)
    assert model.calls == [] and foods[0]["kcal"] == 420 and foods[0]["source"] == "yours"


def chicken(portion):
    return FakeFood([{"name": "chicken breast", "portion": portion, "kcal": 200, "protein": 40, "carbs": 0, "fat": 4,
                      "sugar": 0, "product": ""}])


@pytest.mark.parametrize("text, portion, kcal, protein, source", [
    ("had chicken breast", "2 pieces", 84, 30, "yours"),  # no amount said: your usual as it is
    ("had 3 piece of chicken breast", "3 pieces", 126, 45, "yours"),  # 3 of your usual 2
    ("three chicken breasts", "three chicken breasts", 126, 45, "yours"),
    ("3 piece of chicken breast for lunch its 15g of p each", "3 pieces", 126, 45, "you"),  # your numbers, x3
    ("chicken breast, 50g protein", "2 pieces", 84, 50, "you"),
    ("300g chicken breast", "300 g", 200, 40, "estimate"),  # grams of a usual in pieces: can't compare
])
def test_your_usual_is_worked_out_for_how_much_you_said(fit, text, portion, kcal, protein, source):
    fit.log(fit.draft([{"name": "chicken breast", "portion": "2 pieces (30g)", "kcal": 84, "protein": 30, "fat": 1.5}]))
    said = portion if portion != "2 pieces" else "1 piece"  # what the model reads: theirs, or a typical one
    (food,) = understand(text, fit, fit.db, chicken(said))
    assert (food["kcal"], food["protein"], food["source"]) == (kcal, protein, source)
    assert food["portion"] == (portion if portion != "2 pieces" else "2 pieces (30g)")


@pytest.mark.parametrize("text, given", [
    ("its 15g of p each", {"protein": 15}), ("protein 30g", {"protein": 30}), ("30 protein", {"protein": 30}),
    ("300 kcal and 20 g protein", {"kcal": 300, "protein": 20}), ("calories: 250", {"kcal": 250}),
    ("had 15 protein bar", {}), ("2 eggs", {}),
])
def test_numbers_you_give_are_read(text, given):
    assert given_numbers(text) == given


@pytest.mark.parametrize("text, name, said", [
    ("i had 3 eggs and 2 bread", "egg", (3, "egg", "3 eggs")),
    ("i had 3 eggs and 2 bread", "bread", (2, "bread", "2 bread")),
    ("3 soft boiled eggs and 2 pc of sunshine hokkaido milk bread", "soft boiled eggs", (3, "egg", "3 soft boiled eggs")),
    ("3 soft boiled eggs and 2 pc of sunshine hokkaido milk bread", "Sunshine Hokkaido Milk Bread", (2, "piece", "2 pieces")),
    ("a can of coke", "coke", (1, "can", "1 can")),
    ("3 eggs and rice", "rice", None),  # the 3 is the eggs'
    ("300g chicken breast", "chicken breast", None),  # an amount, not 300 of them
    ("had chicken breast at 3 pm", "chicken breast", None),
    ("bread with tuna, 1 chicken breast", "Bread with Tuna", None),  # the 1 is the chicken breast's
    ("2 strawberries", "strawberry", (2, "strawberry", "2 strawberries")),
])
def test_how_many_of_each_food_you_said(text, name, said):
    assert said_count(text, name) == said


def test_three_eggs_and_two_breads_count_three_and_two(fit):
    """The model may give numbers for one ("each"); your words say how many."""
    fit.log(fit.draft([{"name": "Egg", "portion": "1 large", "kcal": 78, "protein": 6}]))
    model = FakeFood([{"name": "eggs", "portion": "each", "kcal": 70, "protein": 6, "carbs": 0, "fat": 5, "sugar": 0,
                       "product": ""},
                      {"name": "bread", "portion": "each", "kcal": 80, "protein": 3, "carbs": 15, "fat": 1, "sugar": 2,
                       "product": ""}])
    eggs, bread = understand("i had 3 eggs and 2 bread", fit, fit.db, model)
    assert (eggs["name"], eggs["portion"], eggs["kcal"], eggs["protein"], eggs["source"]) == ("Egg", "3 eggs", 234, 18,
                                                                                              "yours")
    assert (bread["portion"], bread["kcal"], bread["protein"]) == ("2 bread", 160, 6)


def test_your_food_is_found_under_a_plural_or_plainer_name(fit):
    fit.log(fit.draft([{"name": "Boiled Egg", "portion": "1 large", "kcal": 78, "protein": 6}]))
    assert fit.known("boiled eggs")["name"] == "Boiled Egg"
    model = FakeFood([{"name": "soft boiled eggs", "portion": "3 eggs", "kcal": 180, "protein": 12, "carbs": 0,
                       "fat": 12, "sugar": 0, "product": ""}])
    (eggs,) = understand("3 soft boiled eggs", fit, fit.db, model)
    assert (eggs["name"], eggs["kcal"], eggs["protein"]) == ("Boiled Egg", 234, 18)
    fit.log(fit.draft([{"name": "Rice", "portion": "1 bowl", "kcal": 200, "protein": 4}]))
    model = FakeFood([{"name": "chicken rice", "portion": "1 plate", "kcal": 600, "protein": 25, "carbs": 75, "fat": 20,
                       "sugar": 2, "product": ""}])
    assert understand("chicken rice", fit, fit.db, model)[0]["kcal"] == 600  # not your plain rice


def test_pieces_of_a_packaged_loaf_are_servings_not_loaves(fit):
    def fetch(url, params):
        return {"hits": [{"product_name": "Hokkaido Milk Bread", "brands": ["Sunshine"], "code": "8888",
                          "countries_tags": ["en:singapore"], "quantity": "400 g", "serving_size": "30 g",
                          "nutriments": {"energy-kcal_100g": 270, "proteins_100g": 8}}]}
    model = FakeFood([{"name": "Sunshine Hokkaido Milk Bread", "portion": "each", "kcal": 90, "protein": 3, "carbs": 15,
                       "fat": 2, "sugar": 3, "product": "Sunshine Hokkaido Milk Bread"}])
    (bread,) = understand("2 pc of sunshine hokkaido milk bread", fit, fit.db, model, fetch=fetch)
    assert (bread["portion"], bread["kcal"], bread["source"]) == ("2 pieces", 162, "database")  # 2 x 30 g, not 400 g


def test_packaged_products_use_open_food_facts_for_the_portion(fit):
    seen = []

    def fetch(url, params):
        seen.append(params["q"])
        return {"hits": [
            {"product_name": "Tofu", "brands": ["NutriSoy"], "code": "9313727000125", "countries_tags": ["en:australia"],
             "nutriments": {"energy-kcal_100g": 130, "proteins_100g": 14.7}},
            {"product_name": "Nutrisoy No sugar", "brands": ["Nutrisoy"], "code": "8888200619312",
             "countries_tags": ["en:singapore"], "quantity": "1 l",
             "nutriments": {"energy-kcal_100g": 37.2, "proteins_100g": 4, "carbohydrates_100g": 1.2, "fat_100g": 1.8,
                            "sugars_100g": 0.8}}]}
    model = FakeFood([{"name": "NutriSoy", "portion": "250 ml", "kcal": 120, "protein": 8, "carbs": 10, "fat": 4,
                       "sugar": 6, "product": "NutriSoy no sugar"}])
    (soy,) = understand("nutrisoy no sugar 250ml", fit, fit.db, model, fetch=fetch)
    assert (soy["kcal"], soy["protein"], soy["source"]) == (93.0, 10.0, "database")
    assert soy["note"] == "Nutrisoy No sugar (Open Food Facts)"  # the Singapore product, not the tofu
    understand("nutrisoy no sugar 250ml", fit, fit.db, model, fetch=fetch)
    assert seen == ["NutriSoy no sugar"]  # looked up once, then cached


def test_offline_lookups_keep_the_estimate(fit):
    def down(url, params):
        raise OSError("no network")
    model = FakeFood([{"name": "Yakult", "portion": "1 bottle", "kcal": 50, "protein": 1, "carbs": 11, "fat": 0,
                       "sugar": 10, "product": "Yakult"}])
    assert understand("a yakult", fit, fit.db, model, fetch=down)[0]["source"] == "estimate"
    assert amount_of("1 carton (250 ml)") == (250, "ml") and amount_of("1L") == (1000, "ml") and amount_of("a bowl") is None


# --- steps -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("2026-10-19 steps 8423 active 412", (8423, 412.0)),
    ("2026-10-19,8423,412", (8423, 412.0)),
    ("Steps: 9 102", (9102, None)),
    ("Steps: 8,423", (8423, None)),
    ("19/10/2026;10234;380.2", (10234, 380.2)),
])
def test_step_lines_from_the_shortcut(line, expected):
    (day, steps, active), = parse_steps(line, date(2026, 10, 19))
    assert (day, steps, active) == (date(2026, 10, 19), *expected)


def test_the_folder_is_read_and_the_highest_count_wins(fit, tmp_path):
    folder = tmp_path / "health"
    folder.mkdir()
    (folder / "steps-2026-10-18.txt").write_text("2026-10-18 steps 11020")
    (folder / "steps-2026-10-19.txt").write_text("steps 6200")  # the date from the file name
    assert import_steps(fit, folder) == 2
    assert fit.steps_on(date(2026, 10, 19)) == 6200
    (folder / "steps-2026-10-19.txt").write_text("2026-10-19 steps 4100")  # an earlier, lower run
    assert import_steps(fit, folder) == 0 and fit.steps_on(date(2026, 10, 19)) == 6200
    assert import_steps(fit, tmp_path / "missing") == 0


# --- the 6pm check ---------------------------------------------------------------------------------------

def test_the_evening_check_plans_with_your_foods(fit):
    fit.set_targets(kcal=1800, protein=140, steps=8000)
    fit.log(fit.draft([{"name": "Chicken breast rice", "portion": "1 plate", "kcal": 520, "protein": 38},
                       {"name": "Laksa", "portion": "1 bowl", "kcal": 600, "protein": 20}]))
    fit.set_steps(fit.today(), 5400)
    text = fit.evening_check()
    assert text.startswith("⏰ Your 6pm check for ")
    assert "🥚 Protein: 58 of 140 g, 82 g to go. Fits your calories: Chicken breast rice (520 kcal, 38 g)" in text
    assert "🚶 Steps: 5,400 of 8,000. A 25-minute walk gets you there." in text and "680 kcal left for today." in text


def test_on_track_or_without_targets_it_stays_quiet(fit):
    assert fit.evening_check() is None  # no targets
    fit.set_targets(kcal=1800, protein=60, steps=5000)
    fit.log(fit.draft([{"name": "Chicken breast rice", "kcal": 520, "protein": 60}]))
    fit.set_steps(fit.today(), 7000)
    assert fit.evening_check() is None


def test_over_on_calories_is_said(fit):
    fit.set_targets(kcal=1500)
    fit.log(fit.draft([{"name": "Buffet", "kcal": 2100, "protein": 70}]))
    assert "600 over" in fit.evening_check()


# --- asking Dorae-3 --------------------------------------------------------------------------------------

@pytest.mark.parametrize("agent, question, intent, slots", [
    ("fitness", "how am i doing today", "fit_day", {"day": "today"}),
    ("fitness", "am i on track", "fit_day", {}),
    ("fitness", "protein left", "fit_day", {}),
    ("fitness", "what did i eat yesterday", "fit_day", {"day": "yesterday"}),
    ("fitness", "calories this week", "fit_week", {}),
    ("fitness", "my targets", "fit_targets", {}),
    ("money", "how much protein today", "fit_day", {"day": "today"}),  # asked elsewhere: pointed to Dorae-3
])
def test_dorae3_lookups(agent, question, intent, slots):
    found = route(agent, question)
    assert (found.agent, found.intent, found.slots) == ("fitness", intent, slots)


@pytest.mark.parametrize("agent, question, span", [
    ("fitness", "based on what i ate the past few weeks, am i hitting my target and what can i improve on", "days:28"),
    ("fitness", "am i hitting my targets", "days:28"),
    ("fitness", "what can i improve on", "days:28"),
    ("fitness", "how can i improve my protein", "days:28"),
    ("fitness", "how have i been doing over the past 2 weeks", "days:14"),
    ("fitness", "how did i do this month", "month:this"),
    ("fitness", "is my protein on track lately", "days:28"),
    ("money", "how was my eating last month", "month:last"),  # asked elsewhere: pointed to Dorae-3
])
def test_reviews_of_several_weeks(agent, question, span):
    found = route(agent, question)
    assert (found.agent, found.intent, found.slots) == ("fitness", "fit_review", {"span": span})


@pytest.mark.parametrize("agent, question", [
    ("fitness", "am i hitting my protein target today"),  # a day's lookup
    ("fitness", "my targets"),
    ("fitness", "set my protein target to 140"),
    ("money", "am i hitting my targets"),  # Dorae-1's budgets
    ("money", "how can i improve my savings"),
])
def test_not_reviews(agent, question):
    found = route(agent, question)
    assert found is None or found.intent != "fit_review"


def test_a_review_against_your_targets(fit):
    today = fit.today()
    fit.set_targets(kcal=2000, protein=120, steps=10000)
    for back in range(1, 9):  # 8 days logged: protein short, three days over on laksa, one day partly logged
        day = today - timedelta(days=back)
        if back == 4:
            eat(fit, day, 500, 20)
            continue
        fit.log(fit.draft([{"name": "Laksa" if back in (2, 5, 6) else "Chicken rice", "portion": "",
                            "kcal": 2500 if back in (2, 5, 6) else 1900,
                            "protein": 100, "carbs": 200, "fat": 70}], day))
        fit.set_steps(day, 6000)
    eat(fit, today, 3000, 200)  # today isn't over: left out
    fit.remember({"name": "Greek yogurt", "portion": "150 g", "kcal": 130, "protein": 15, "carbs": 6, "fat": 4, "sugar": 5})
    card = coach.review_card(fit, "days:28")
    p = card["payload"]
    assert card["kind"] == "fitness_review" and p["label"].startswith("Since you started logging")  # not 28 empty days
    assert "you logged 8 of 8 days (7 fully)" in card["text"] and p["light"] == [[coach.short_day(today - timedelta(days=4)), 500]]
    assert p["rows"][0][:4] == ["Calories", "2,157", "2,000 kcal", "on"] and "over 3" in p["rows"][0][4]
    assert p["rows"][1][:4] == ["Protein", "100", "120 g", "under"] and p["rows"][1][4] == "hit on 0 of 7 days"
    assert p["rows"][2][:4] == ["Steps", "6,000", "10,000", "under"]
    tips = "\n".join(p["tips"])
    assert "Protein: about 20 g a day short" in tips and "Greek yogurt" in tips
    assert "Calories: over 2,000 on 3 of 7 days, mostly from laksa (3×)" in tips
    assert "A 40-minute walk a day closes it." in tips and len(p["tips"]) == 3
    assert "To improve:\n1. Protein" in card["text"]


def test_a_review_with_too_little_logged_says_so(fit):
    fit.set_targets(kcal=2000, protein=120)
    eat(fit, fit.today() - timedelta(days=1), 1900, 130)
    assert coach.review_card(fit)["payload"]["tips"] == [
        "Only 1 fully logged day so far: log every meal for a week and I can say much more."]


def test_a_review_with_nothing_logged(tmp_path):
    fit = Fitness(Database(tmp_path / "empty.db"), "Asia/Singapore")
    assert "set your targets on the Fitness page" in coach.review_card(fit)["text"]
    fit.set_targets(protein=120)
    assert coach.review_card(fit)["text"].endswith("nothing fully logged yet. Tell me what you eat for a few days, then ask again.")


def test_dorae3_answers_a_review_question_in_chat_and_on_telegram(settings):
    client = chat_client(settings)
    rt = client.app.state.runtime
    rt.brain.fit.set_targets(kcal=2000, protein=120)
    for back in range(1, 5):
        eat(rt.brain.fit, rt.brain.fit.today() - timedelta(days=back), 1950, 100)
    html = client.post("/chat/fitness/ask", data={
        "q": "based on what i ate the past few weeks, am i hitting my target and what can i improve on"}).json()["html"]
    assert "calories on target, protein 20 g a day short" in html and "To improve:" in html and "Most calories from" in html
    assert "Your daily targets" not in html
    card = coach.review_card(rt.brain.fit)
    assert "• Protein: 100 of 120 g a day, hit on 0 of 4 days" in rt.phone.plain(card)


def test_how_am_i_doing_stays_with_money_in_dorae1s_chat():
    assert route("money", "how am i doing on my budget").intent == "budget"
    assert route("money", "how am i doing") is None


def chat_client(settings):
    client = TestClient(create_app(settings), headers={"x-requested-with": "fetch"})
    client.app.state.runtime.brain.food_backend = FakeFood()
    return client


def test_logging_a_meal_in_dorae3s_chat(settings):
    client = chat_client(settings)
    client.post("/chat/fitness/ask", data={"q": "set my protein target to 140"})
    html = client.post("/chat/fitness/ask", data={"q": "chicken rice and kopi siew dai"}).json()["html"]
    assert "Here&#39;s what I worked out: 710 kcal and 27 g protein" in html and "food-card" in html
    batch = re.search(r"/food/([0-9a-f]{8})/log", html)[1]
    html = client.post(f"/food/{batch}/log").json()["html"]
    assert "Logged: 710 kcal, 27 g protein. Today so far: 710 kcal, 27 of 140 g protein." in html
    html = client.post("/chat/fitness/ask", data={"q": "how am i doing today?"}).json()["html"]
    assert "710 kcal, 27 of 140 g protein" in html and "fit-meter" in html
    assert "Chicken rice" in client.get("/fitness").text


def test_the_fitness_page_suggests_targets_from_your_profile(settings):
    client = chat_client(settings)
    page = client.post("/fitness/profile", data={"weight_kg": "70", "height_cm": "175", "age": "23", "sex": "male",
                                                 "activity": "light", "goal": "lose"}, follow_redirects=True).text
    assert 'name="kcal" inputmode="numeric" value="1850"' in page and "Suggested from your profile" in page
    client.post("/fitness/targets", data={"kcal": "1850", "protein": "140", "steps": "8000", "back": "/fitness"})
    assert client.app.state.runtime.brain.fit.targets() == {"kcal": 1850, "protein": 140, "steps": 8000}


# --- Telegram and the jobs ---------------------------------------------------------------------------------

def test_logging_food_from_telegram(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    rt.brain.food_backend = FakeFood()
    text, buttons = rt.phone.answer("ate chicken rice and kopi siew dai")
    assert text.startswith("🍽 Here's what I worked out:\n• Chicken rice (1 plate): 600 kcal, 25 g protein")
    assert "Total: 710 kcal, 27 g protein" in text and [b[0] for b in buttons[0]] == ["✅ Log", "✏️ Edit", "✖ Cancel"]
    said, kept = rt.button(buttons[0][0][1])
    assert said.startswith("✅ Logged. Today: 710 kcal") and kept == [[("✏️ Edit", buttons[0][1][1])]]
    assert rt.button(buttons[0][0][1]) == "That one's already logged (or was cancelled)."
    assert rt.phone.answer("how am i doing today?")[0].startswith("Today, ")


def test_telegram_asks_the_model_about_a_plain_list_of_foods(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = FakeFood()
    text, buttons = rt.phone.answer("chicken rice and kopi siew dai")
    assert text.startswith("🍽 Here's what I worked out:") and buttons
    rt.brain.food_backend = nothing = FakeFood()
    nothing.foods = []
    text, buttons = rt.phone.answer("had a meeting with sam about the project")
    assert buttons is None and not text.startswith("🍽")  # not food: what works here instead
    calls = len(nothing.calls)
    rt.phone.answer("ok")  # chatter never waits for the model
    assert len(nothing.calls) == calls


def test_yesterdays_food_from_telegram(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = FakeFood()
    text, buttons = rt.phone.answer("yesterday i had chicken rice")
    assert text.startswith("🍽 Here's what I worked out for yesterday:")
    rows = rt.brain.fit.batch(buttons[0][0][1].split(":")[1])
    assert {r["day"] for r in rows} == {(rt.brain.today() - timedelta(days=1)).isoformat()}


def test_editing_food_on_telegram(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = FakeFood()
    _, buttons = rt.phone.answer("ate chicken rice and kopi siew dai")
    edit_button = buttons[0][1][1]
    said, kept = rt.button(edit_button)
    assert said == "✏️ Send the new numbers, like “protein 45, kcal 160”, starting with the food, like “Kopi siew dai: kcal 80”."
    assert kept == buttons  # still a draft: Log, Edit and Cancel stay
    assert rt.phone.answer("kcal 500")[0] == "Which one? Start with the food, like “Kopi siew dai: kcal 500”."
    text, again = rt.phone.answer("chicken rice: protein 30, kcal 650")
    assert text.startswith("✏️ Updated:\n• Chicken rice (1 plate): 650 kcal, 30 g protein") and again == buttons
    assert rt.phone.answer("protein 99")[0] != text  # Edit is over: that's not an edit any more
    rt.button(buttons[0][0][1])  # Log
    assert rt.brain.fit.known("chicken rice")["kcal"] == 650  # what you changed is remembered
    # after logging, Edit still changes it, and today's totals with it
    rt.button(edit_button)
    text, kept = rt.phone.answer("kopi siew dai: 90 kcal")
    assert "Kopi siew dai (1 cup): 90 kcal" in text and "Today: 740 kcal" in text
    assert kept == [[("✏️ Edit", edit_button)]]
    assert rt.brain.fit.totals(rt.brain.fit.today()).kcal == 740


def test_a_new_meal_after_edit_is_a_new_meal(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = FakeFood()
    _, buttons = rt.phone.answer("ate chicken rice")
    rt.button(buttons[0][1][1])
    text, _ = rt.phone.answer("ate 2 eggs, 12g protein")
    assert text.startswith("🍽 Here's what I worked out")


@pytest.mark.parametrize("text", ["a protein shake", "protein shake", "whey and a banana",
                                  "chicken rice for lunch today", "laksa for dinner just now"])
def test_food_that_looks_like_a_question_is_logged_from_telegram(settings, text):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = food = FakeFood()
    reply, buttons = rt.phone.answer(text)
    assert reply.startswith("🍽 Here's what I worked out:") and buttons and food.calls


def test_a_protein_bar_question_is_about_the_protein_bar():
    assert coach.nutrition_subject("how many calories in a protein bar?") == "a protein bar"


def test_same_as_yesterday_on_telegram(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = food = FakeFood()
    fit = rt.brain.fit
    assert rt.phone.answer("same as yesterday")[0].startswith("Nothing was logged yesterday")
    yesterday = datetime.now(fit.tz).replace(hour=8, minute=0) - timedelta(days=1)
    logged_at(fit, yesterday, "Kaya toast", 350, 10)
    text, buttons = rt.phone.answer("same as yesterday")
    assert text.startswith("🍽 Same as yesterday:\n• Kaya toast") and buttons[0][0][0] == "✅ Log"
    assert [r["day"] for r in fit.batch(buttons[0][0][1].split(":")[1])] == [fit.today().isoformat()]
    logged_at(fit, yesterday.replace(hour=19), "Bak kut teh", 450, 28)
    assert "Bak kut teh" in rt.phone.answer("had the same dinner as yesterday")[0]  # the evening one
    text, buttons = rt.phone.answer("same as yesterday")
    assert text.startswith("Which one again? Yesterday you logged:") and len(buttons) == 3
    assert buttons[1][0][0] == "Bak kut teh" and buttons[2][0][0] == "All of it"
    text, kept = rt.button(buttons[2][0][1])
    assert "Kaya toast" in text and "Bak kut teh" in text and "Total: 800 kcal" in text and kept[0][0][0] == "✅ Log"
    assert food.calls == []  # copied from your log, no model


def test_a_meal_that_cant_be_told_apart_is_chosen(settings):
    rt = Runtime(settings, messenger=FakeBot())
    fit = rt.brain.fit
    logged_at(fit, datetime.now(fit.tz).replace(hour=22, minute=0) - timedelta(days=1), "Nasi lemak", 500, 12)
    text, buttons = rt.phone.answer("same lunch as yesterday")  # the whole day, logged at night: not surely lunch
    assert text == "Which one again? Yesterday you logged:\n• Nasi lemak: 500 kcal"
    assert [b[0] for row in buttons for b in row] == ["Nasi lemak", "✖ None"]
    assert rt.phone.answer("had my usual breakfast")[0].startswith("I don't know your usual breakfast yet. Which one")


def test_your_usual_breakfast_on_telegram(settings):
    rt = Runtime(settings, messenger=FakeBot())
    fit = rt.brain.fit
    for back in (1, 2, 4):
        logged_at(fit, datetime.now(fit.tz).replace(hour=8, minute=0) - timedelta(days=back), "Kaya toast", 350, 10)
    text, buttons = rt.phone.answer("had my usual breakfast")
    assert text.startswith("🍽 Your usual breakfast:\n• Kaya toast") and buttons


def test_messages_sent_while_the_bot_was_off(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = FakeFood()
    tz = rt.brain.fit.tz
    sent = datetime.now(tz) - timedelta(days=1)
    text, buttons = rt.phone.answer("ate chicken rice", sent=sent)
    assert text.startswith(f"🍽 From your message at {short_clock(f'{sent:%H:%M}')} on ") and "for yesterday:" in text
    assert {r["day"] for r in rt.brain.fit.batch(buttons[0][0][1].split(":")[1])} == {sent.date().isoformat()}
    text, buttons = rt.phone.answer("remind me to call mom in 10 minutes", sent=datetime.now(tz) - timedelta(hours=2))
    assert text.startswith("I was offline when you sent “remind me to call mom in 10 minutes”") and buttons is None
    assert rt.phone.answer("ate chicken rice", sent=datetime.now(tz))[0].startswith("🍽 Here's what I worked out")


def test_telegram_says_when_the_model_is_down(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_model = lambda: None
    assert rt.phone.answer("chicken rice")[0].startswith("I need the local model")  # not the list of commands

    class Stopped(FakeFood):
        def complete_json(self, system, user, schema):
            raise httpx.ConnectError("refused")
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = Stopped()
    assert rt.phone.answer("ate chicken rice")[0].startswith("I need the local model")
    assert rt.brain.food_backend is None  # looked for (and started) again next time


def test_the_6pm_check_comes_once_and_reaches_telegram(settings, tmp_path):
    bot = FakeBot()
    rt = Runtime(dataclasses.replace(settings, fitness_check_time="18:00"), messenger=bot)
    rt.brain.fit.set_targets(protein=120)
    today = datetime.now(SG).replace(hour=0, minute=0, second=0, microsecond=0)
    assert not rt.fitness_tick(today.replace(hour=17))
    assert rt.fitness_tick(today.replace(hour=18, minute=5))
    assert not rt.fitness_tick(today.replace(hour=19))
    assert bot.sent[0][0].startswith("⏰ Your 6pm check") and "120 g to go" in bot.sent[0][0]
    assert rt.db.last_message("fitness")["text"] == bot.sent[0][0]


def test_opening_dorae3_shows_your_day_not_the_calendar_briefing(settings):
    client = chat_client(settings)
    client.app.state.runtime.brain.fit.set_targets(protein=120)
    page = client.get("/chat/fitness").text
    assert "Today, " in page and "nothing logged yet" in page
    assert "need your OK" not in page.split('id="thread"')[1].split("</section>")[0]


def test_steps_are_read_from_every_watched_folder(fit, tmp_path):
    icloud, onedrive = tmp_path / "iCloudDrive" / "Shortcuts" / "Doraemon" / "health", tmp_path / "OneDrive" / "health"
    icloud.mkdir(parents=True)
    (icloud / "steps-2026-10-19.txt").write_text("2026-10-19 steps 8423")
    assert import_steps(fit, f"{icloud};{onedrive}") == 1  # the missing OneDrive folder is just skipped
    assert fit.steps_on(date(2026, 10, 19)) == 8423


def test_steps_are_read_from_a_file_or_a_folder_shortcuts_named_like_one(fit, tmp_path):
    file = tmp_path / "Steps" / "today.txt"
    file.parent.mkdir()
    file.write_text("2026-10-19 steps 8423")
    assert import_steps(fit, str(file)) == 1 and fit.steps_on(date(2026, 10, 19)) == 8423
    # Shortcuts' Save File made "steps.txt" a folder and named the file after the count, with no date anywhere
    odd = tmp_path / "Steps" / "steps.txt" / "1730.txt"
    odd.parent.mkdir()
    odd.write_text("1730\n")
    assert import_steps(fit, f'"{odd.parent}"') == 1  # quotes from "Copy as path" are fine
    assert fit.steps_on(datetime.fromtimestamp(odd.stat().st_mtime, fit.tz).date()) == 1730  # the day it was saved


def test_the_steps_path_is_changed_on_the_fitness_page(settings, tmp_path):
    folder = tmp_path / "Steps" / "steps.txt"
    folder.mkdir(parents=True)
    (folder / "1730.txt").write_text("1730")
    client = chat_client(settings)
    page = client.post("/fitness/health", data={"path": f'"{folder}"'}).text
    assert "Today so far: 1,730 steps." in page and str(folder) in page and "Use the default places" in page
    assert client.app.state.runtime.steps_tick() == 0  # the service reads the same path (nothing new since)
    page = client.post("/fitness/health", data={"reset": "1"}).text
    assert "Back to the default places. Nothing there yet" in page  # the test's default folder doesn't exist
    assert str(tmp_path / "health") in page and "Use the default places" not in page


def eat(fit, day, kcal, protein, carbs=0, fat=0):
    fit.log(fit.draft([{"name": "Meal", "portion": "", "kcal": kcal, "protein": protein, "carbs": carbs, "fat": fat}], day))


def test_the_charts_average_over_the_days_you_logged(fit):
    today = fit.today()
    eat(fit, today, 600, 30, 60, 20)
    eat(fit, today, 400, 20, 40, 10)  # a day's meals add up
    eat(fit, today - timedelta(days=7), 2000, 100)  # always last week
    fit.set_steps(today, 5000)
    fit.set_steps(today - timedelta(days=1), 7000)
    days = fit.trend("days")["buckets"]
    assert len(days) == 14 and days[-1]["partial"] and not days[-2]["partial"]
    assert (days[-1]["kcal"], days[-1]["protein"], days[-1]["steps"]) == (1000, 50, 5000)
    assert days[-2]["kcal"] is None and days[-2]["steps"] == 7000  # not logged isn't zero
    weeks = fit.trend("weeks")["buckets"]
    assert len(weeks) == 12 and (weeks[-1]["kcal"], weeks[-1]["logged"]) == (1000, 1) and weeks[-2]["kcal"] == 2000
    months = fit.trend("months")
    assert len(months["buckets"]) == 6 and months["buckets"][-1]["partial"]
    assert months["whole"]["kcal"] == 3000 and months["logged"] == 2
    assert fit.trend("years")["view"] == "days"


def test_the_charts_shapes(fit):
    from doraemon.web import charts
    eat(fit, fit.today(), 600, 30, 60, 20)  # 120 + 240 + 180 kcal from protein, carbs and fat
    trend = fit.trend("days")
    split = charts.macro_split(trend)
    assert [l["pct"] for l in split["legend"]] == ["22", "44", "33"] and split["kcal"] == 600  # the centre: as logged
    cols = charts.fit_columns(trend, "kcal", 2000, " kcal")
    assert cols["budget"]["label"] == "Target 2,000 kcal" and not cols["empty"]
    assert cols["cols"][-1]["cap"] == "600" and all(c["cap"] == "" for c in cols["cols"][:-1])  # 14: only the latest
    assert cols["cols"][-1]["tip"].endswith(": 600 kcal · target 2,000 kcal")
    assert charts.fit_columns(trend, "steps", None, " steps")["empty"]
    stacked = charts.macro_columns(trend)["cols"]
    assert [b["color"] for b in stacked[-1]["bars"]] == list(charts.MACRO_COLORS.values()) and stacked[0]["bars"] == []


def test_the_fitness_page_shows_the_charts(settings):
    client = chat_client(settings)
    eat(client.app.state.runtime.brain.fit, client.app.state.runtime.brain.fit.today(), 600, 30, 60, 20)
    page = client.get("/fitness?view=months").text
    assert 'class="tab" aria-current=page>Months' in page and "The average a day, over the days you logged" in page
    assert "Where your calories come from" in page and "22% of calories" in page and "No steps in these 6 months" in page
    assert "a day over 1 day logged: protein 30 g (120 kcal)" in page  # the stacked column's tip


def test_the_fitness_page_shows_which_folders_it_watches(settings, tmp_path):
    folder = tmp_path / "icloud-health"
    folder.mkdir()
    client = chat_client(dataclasses.replace(settings, health_folder=f"{folder};{tmp_path / 'nope'}"))
    page = client.get("/fitness").text
    assert str(folder) in page and "✓ found" in page and "not there yet" in page


# --- ideas for what to eat ----------------------------------------------------------------------------

@pytest.mark.parametrize("text, ideas, targets", [
    ("suggest me some foods to eat so that i can hit my targets", True, True),  # ideas win over targets
    ("what should i eat for dinner", True, False),
    ("any snack ideas to hit my protein?", True, False),
    ("suggest my targets", False, True),
    ("had chicken rice for lunch", False, False),
])
def test_ideas_for_food_are_told_apart_from_targets(text, ideas, targets):
    assert bool(coach.FOOD_IDEAS.search(text)) is ideas and bool(coach.SUGGEST.search(text)) is targets
    assert coach.food_request("what should i eat for dinner", True) is None
    assert coach.food_request("had chicken rice for lunch", True) == "chicken rice"


class PlanningModel:
    """A chat model that calls meal_ideas, then answers from what it returned."""

    def __init__(self):
        self.seen = []

    def complete_json(self, system, user, schema):
        return json.dumps({"topic": "fitness"})

    def chat(self, messages, tools):
        self.seen.append(messages)
        if len(self.seen) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "meal_ideas", "arguments": {}}}]}
        facts = json.loads(messages[-1]["content"])
        plan = facts["plans"][0]
        return {"role": "assistant", "content": f"Have {' + '.join(plan['foods'])}: {plan['kcal']} kcal, {plan['protein']} g protein."}


def test_asking_what_to_eat_goes_to_the_model_with_whats_left(settings):
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    from doraemon.chat import AgentChat
    model = PlanningModel()
    brain.chat = AgentChat(brain, model)
    brain.fit.set_targets(kcal=1800, protein=140)
    brain.fit.log(brain.fit.draft([{"name": "Chicken breast rice", "portion": "1 plate", "kcal": 520, "protein": 38}]))
    html = client.post("/chat/fitness/ask", data={"q": "suggest me some foods to eat so that i can hit my targets"}).json()["html"]
    facts = json.loads(model.seen[1][-1]["content"])
    plan = facts["plans"][0]
    assert brain.last_route == "model" and f"Have {' + '.join(plan['foods'])}: {plan['kcal']} kcal" in html
    assert (facts["kcal_left"], facts["protein_left"]) == (1280, 102) and plan["kcal"] <= 1280
    assert "foods" not in facts  # the plans are the answer
    assert facts["status"] == "calories: 1280 left; protein: 102 g still to go"
    assert "Fits what's left today" in html  # the options card under the answer


def test_without_the_model_the_app_still_gives_ideas(settings):
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    brain.fit.set_targets(kcal=1800, protein=140)
    html = client.post("/chat/fitness/ask", data={"q": "what should i eat for dinner?"}).json()["html"]
    assert brain.last_route == "ideas" and "You have 1,800 kcal and 140 g protein left today." in html
    rt = Runtime(settings, messenger=FakeBot())
    assert rt.phone.answer("what should I eat for dinner?")[0].startswith("You have ")


def test_with_calories_used_up_the_ideas_turn_to_the_leanest_protein(fit):
    fit.set_targets(kcal=1850, protein=140)
    fit.log(fit.draft([{"name": "Buffet lunch", "kcal": 1935, "protein": 79}]))
    facts = coach.ideas_facts(fit)
    assert facts["status"] == "calories: 85 OVER the target; protein: 61 g still to go"
    assert facts["foods"] and facts["foods"][0]["name"] == "Tuna in water (1 can)"  # 26 g for 120 kcal
    assert facts["how"].startswith("Calories are used up but protein is short.")


def test_meal_plans_fit_the_calories_and_add_up_here():
    options = [{"name": "Kaya toast set", "kcal": 480, "protein": 18, "yours": True},
               {"name": "Nasi lemak", "kcal": 500, "protein": 12, "yours": True},
               {"name": "Chicken breast (150 g)", "kcal": 250, "protein": 45, "yours": False},
               {"name": "Greek yogurt (150 g)", "kcal": 130, "protein": 15, "yours": False},
               {"name": "Fish soup with rice", "kcal": 480, "protein": 32, "yours": True}]
    plans = coach.meal_plans(options, kcal_left=900, protein_left=80)
    best = plans[0]
    assert best["kcal"] <= 900 and best["protein"] >= 80  # 45 + 32 = 77 alone isn't enough: the yogurt makes 92
    assert set(best["foods"]) == {"Chicken breast (150 g)", "Fish soup with rice", "Greek yogurt (150 g)"}
    assert (best["kcal"], best["protein"]) == (860, 92)
    assert all(p["kcal"] <= 900 for p in plans) and len(plans) > 1


# --- questions about food ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text, subject, judgement", [
    ("how much will a bowl of greek yogurt be in cals and protein", "a bowl greek yogurt", False),
    ("how many calories in chicken rice", "chicken rice", False),
    ("calories in 2 slices of white bread", "2 slices white bread", False),
    ("how much protein does tofu have?", "tofu", False),
    ("which has more protein, tofu or eggs?", None, True),
    ("is milo high in sugar?", None, True),
    ("how much protein have i had today", None, False),  # your day, not a food
    ("calories this week", None, False),
])
def test_food_questions_are_told_apart(text, subject, judgement):
    assert coach.nutrition_subject(text) == subject
    assert bool(coach.FOOD_QUESTION.search(text)) is judgement


def test_a_food_question_answers_with_numbers_and_offers_log(settings):
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    brain.food_backend = FakeFood([{"name": "Greek yogurt", "portion": "1 bowl (200 g)", "kcal": 190, "protein": 18,
                                    "carbs": 8, "fat": 9, "sugar": 7, "product": ""}])
    html = client.post("/chat/fitness/ask",
                       data={"q": "how much will a bowl of greek yogurt be in cals and protein"}).json()["html"]
    assert brain.last_route == "nutrition"
    assert "Greek yogurt, 1 bowl (200 g): about 190 kcal, 18 g protein, 8 g carbs, 9 g fat, 7 g sugar (an estimate)." in html
    assert brain.fit.totals(brain.fit.today()).kcal == 0  # asking isn't eating
    batch = re.search(r"/food/([0-9a-f]{8})/log", html)[1]
    client.post(f"/food/{batch}/log")
    assert brain.fit.totals(brain.fit.today()).kcal == 190


class ComparingModel:
    """Calls the nutrition tool, then answers from it."""

    def __init__(self):
        self.seen = []

    def complete_json(self, system, user, schema):
        return json.dumps({"topic": "fitness"})

    def chat(self, messages, tools):
        self.seen.append(messages)
        if len(self.seen) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "nutrition", "arguments": {"food": "100 g tofu, 1 egg"}}}]}
        foods = json.loads(messages[-1]["content"])["foods"]
        best = max(foods, key=lambda f: f["protein"])
        return {"role": "assistant", "content": f"{best['name']} has more: {best['protein']} g."}


def test_comparing_foods_goes_to_the_model_with_looked_up_numbers(settings):
    from doraemon.chat import AgentChat
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    brain.food_backend = FakeFood([{"name": "Tofu", "portion": "100 g", "kcal": 76, "protein": 8, "carbs": 2, "fat": 5,
                                    "sugar": 0, "product": ""},
                                   {"name": "Egg", "portion": "1 large", "kcal": 72, "protein": 6, "carbs": 0, "fat": 5,
                                    "sugar": 0, "product": ""}])
    brain.chat = AgentChat(brain, ComparingModel())
    html = client.post("/chat/fitness/ask", data={"q": "which has more protein, tofu or eggs?"}).json()["html"]
    assert brain.last_route == "model" and brain.chat.last_tools == ["nutrition"] and "Tofu has more: 8 g." in html


def test_food_questions_on_telegram(settings):
    bot = FakeBot()
    rt = Runtime(settings, messenger=bot)
    rt.brain.food_backend = FakeFood([{"name": "Coke", "portion": "1 can", "kcal": 140, "protein": 0, "carbs": 39,
                                       "fat": 0, "sugar": 39, "product": ""}])
    text, buttons = rt.phone.answer("how many calories in a can of coke")
    assert text.startswith("Coke, 1 can: about 140 kcal") and buttons[0][0][0] == "✅ Log it"
    assert rt.button(buttons[0][2][1]) == "✖ Not logged."


# --- photos: nutrition labels and food --------------------------------------------------------------------

LABEL = {"kind": "label", "product": "NutriSoy Fresh Soya Milk", "basis": "100 ml", "serving_size": "250 ml",
         "pack_size": "1L", "kcal": 39, "kj": 0, "protein": 3.5, "carbs": 2.2, "fat": 1.8, "sugar": 0.5, "foods": []}
BAR = {"product": "Granola bars", "basis": "serving", "serving_size": "42 g", "pack_size": "5 x 42 g", "kcal": 0,
       "kj": 800, "protein": 3, "carbs": 29, "fat": 7, "sugar": 11}


class FakeEyes:
    """The model looking at a photo: one canned reading, and what it was shown."""
    name = "fake"

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def complete_json(self, system, user, schema, images=None):
        self.seen.append((user, images))
        return json.dumps(self.answer)


@pytest.mark.parametrize("reading, said, portion, kcal", [
    (LABEL, None, "1 serving (250 ml)", 97.5),
    (LABEL, "330ml", "330 ml", 128.7),
    (LABEL, "2 servings", "2 servings (500 ml)", 195),
    (LABEL, "had 2 glasses", "2 servings (500 ml)", 195),
    (LABEL, "half the carton", "half a pack (500 ml)", 195),
    (LABEL, "drank the whole thing", "1 pack (1000 ml)", 390),
    (BAR, None, "1 serving (42 g)", 191.2),  # 800 kJ
    (BAR, "2 bars", "2 servings (84 g)", 382.4),  # bars of the 5 x 42 g box, not 2 boxes
    (BAR, "60 g", "1.43 servings (60 g)", 273.4),
    (BAR, "330 ml", "1 serving (42 g)", 191.2),  # ml of a label in grams: the usual amount
])
def test_the_app_works_out_a_labels_numbers_for_what_you_had(reading, said, portion, kcal):
    food = label_food(reading, said)
    assert (food["portion"], food["kcal"], food["source"]) == (portion, kcal, "label")


def test_a_label_without_numbers_is_no_food():
    assert label_food({**LABEL, "kcal": 0, "protein": 0}) is None


@pytest.mark.parametrize("text, amount", [
    ("330ml", {"size": (330.0, "ml")}), ("1.5L", {"size": (1500.0, "ml")}), ("2 servings", {"servings": 2.0}),
    ("actually half the pack", {"packs": 0.5}), ("I had the whole thing", {"packs": 1.0}),
    ("2 servings of rice", None), ("had chicken rice", None), ("how many calories", None), ("yes", None),
])
def test_only_a_bare_amount_answers_a_label(text, amount):
    assert amount_reply(text) == amount


def test_a_label_photo_in_dorae3s_chat(settings):
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    brain.food_backend = eyes = FakeEyes(LABEL)
    html = client.post("/chat/fitness/photo", files={"photo": ("label.jpg", b"jpeg bytes", "image/jpeg")},
                       data={"q": ""}).json()["html"]
    assert eyes.seen[0][1] == [b"jpeg bytes"]
    assert "From the label: NutriSoy Fresh Soya Milk, 1 serving (250 ml): 98 kcal, 9 g protein." in html
    assert "from your label photo" in html and brain.fit.totals(brain.fit.today()).kcal == 0
    photo = re.search(r'src="(/photos/[0-9a-f]{32}\.jpg)"', html)[1]
    assert client.get(photo).content == b"jpeg bytes"
    first = re.search(r"/food/([0-9a-f]{8})/log", html)[1]
    # "2 servings" redoes the sums, without the model
    html = client.post("/chat/fitness/ask", data={"q": "2 servings"}).json()["html"]
    assert brain.last_route == "label" and len(eyes.seen) == 1
    assert "From the label: NutriSoy Fresh Soya Milk, 2 servings (500 ml): 195 kcal, 18 g protein." in html
    assert {r["status"] for r in brain.fit.batch(first)} == {"cancelled"}
    client.post(f"/food/{re.search(r'/food/([0-9a-f]{8})/log', html)[1]}/log")
    assert brain.fit.totals(brain.fit.today()).kcal == 195


def test_only_photos_are_taken_and_kept(settings):
    client = chat_client(settings)
    assert client.post("/chat/fitness/photo", files={"photo": ("a.txt", b"hello", "text/plain")}).status_code == 400
    assert client.get("/photos/..%2Fd.db").status_code == 404
    assert client.get("/photos/" + "0" * 32 + ".jpg").status_code == 404


def test_a_food_photo_is_estimated_with_what_you_said(settings, fit):
    client = chat_client(settings)
    brain = client.app.state.runtime.brain
    brain.food_backend = eyes = FakeEyes({**LABEL, "kind": "food", "foods": [
        {"name": "Chicken rice", "portion": "1 plate", "kcal": 450, "protein": 22, "carbs": 40, "fat": 18, "sugar": 1,
         "product": ""}]})
    html = client.post("/chat/fitness/photo", files={"photo": ("lunch.jpg", b"jpeg", "image/jpeg")},
                       data={"q": "no rice"}).json()["html"]
    assert eyes.seen[0][0].endswith('They said: "no rice"')
    assert "From your photo: about 450 kcal and 22 g protein. Photos give rough numbers" in html
    assert "no rice" in html and "estimate" in html


def test_a_photo_of_something_else(settings):
    client = chat_client(settings)
    client.app.state.runtime.brain.food_backend = FakeEyes({**LABEL, "kind": "neither"})
    html = client.post("/chat/fitness/photo", files={"photo": ("cat.jpg", b"jpeg", "image/jpeg")}).json()["html"]
    assert "I couldn&#39;t see food or a nutrition label in that photo." in html


def test_label_photos_on_telegram(settings):
    rt = Runtime(settings, messenger=FakeBot())
    rt.brain.food_backend = FakeEyes(LABEL)
    text, buttons = rt.phone.photo(b"jpeg", "drank half the carton")
    assert text.startswith("📷 From the label:\n• NutriSoy Fresh Soya Milk, half a pack (500 ml): 195 kcal")
    assert "Reply like “330 ml”" in text and buttons[0][0] == ("✅ Log", buttons[0][0][1])
    text, buttons = rt.phone.answer("330 ml")
    assert "NutriSoy Fresh Soya Milk (330 ml): 129 kcal" in text
    assert rt.button(buttons[0][0][1])[0].startswith("✅ Logged. Today: 129")
    # a label from long ago: "2 servings" is something else
    old = json.loads(rt.db.get_setting("fitness:last_label"))
    rt.db.set_setting("fitness:last_label", json.dumps({**old, "at": "2026-01-01T00:00:00+00:00"}))
    assert "From the label" not in rt.phone.answer("2 servings")[0]


# --- Dorae-3 asking first (doraemon.nudges) ---------------------------------------------------------------------

def pay(db, merchant, at, amount="7.20", category="dining", refund=0):
    cur = db.conn.execute("INSERT INTO transactions (gmail_id, source, merchant, purchased_at, amount, currency, category, "
                          "is_refund) VALUES ('g', 'bank_alert', ?, ?, ?, 'SGD', ?, ?)",
                          (merchant, at.isoformat(), amount, category, refund))
    db.conn.commit()
    return cur.lastrowid


def logged_at(fit, at, name="Chicken rice", kcal=600, protein=25):
    batch = fit.draft([{"name": name, "portion": "1 plate", "kcal": kcal, "protein": protein}], at.date())
    fit.log(batch)
    fit.db.conn.execute("UPDATE food_log SET logged_at = ? WHERE batch = ?", (at.astimezone(SG).isoformat(), batch))
    fit.db.conn.commit()


def nudge_runtime(settings):
    bot = FakeBot()
    rt = Runtime(dataclasses.replace(settings, fitness_nudges=True), messenger=bot)
    rt.brain.food_backend = FakeFood()
    return rt, bot


@pytest.mark.parametrize("merchant, name", [("MOS BURGER-JURONG POINT SINGAPORE SG", "Mos Burger"),
                                            ("McDonalds 930013 Singapore SG", "Mcdonalds"),
                                            ("STRIPE PAYMENTS SINGAPORE PTE. LTD.", "Stripe Payments")])
def test_merchant_names(merchant, name):
    assert nudges.merchant_name(merchant) == name


def test_a_payment_at_a_food_place_asks_what_you_had_and_offers_last_time(settings):
    rt, bot = nudge_runtime(settings)
    now = datetime.now(SG).replace(hour=13, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7, hours=1)
    pay(rt.db, "McDonald's Jurong Bowl", week_ago)
    logged_at(rt.brain.fit, week_ago + timedelta(minutes=40), "Double cheeseburger", 450, 25)  # what you had then
    assert not rt.meal_prompt_tick(now)  # the first time only notes where it's up to
    pay(rt.db, "McDonalds 930013 Singapore SG", now - timedelta(minutes=20), "9.25")
    assert rt.meal_prompt_tick(now)
    text, buttons = bot.sent[-1]
    assert text.startswith("🍽 Lunch at Mcdonalds? (SGD 9.25 at 12:40pm)") and "Last time there: Double cheeseburger" in text
    assert buttons[0][0][0] == "✅ Double cheeseburger again" and buttons[1][0][1].startswith("fmeal:")
    reply = rt.button(buttons[0][0][1])
    assert reply[0].startswith("✅ Logged. Today: 450") and rt.brain.fit.totals(now.date()).kcal == 450
    pay(rt.db, "McDonald's Jurong Bowl", now - timedelta(minutes=20), "9.25")  # the twin alert: asked once only
    assert not rt.meal_prompt_tick(now + timedelta(minutes=15))


def test_a_reply_to_the_meal_question_logs_on_that_day(settings):
    rt, bot = nudge_runtime(settings)
    now = datetime.now(SG).replace(hour=19, minute=30, second=0, microsecond=0)
    rt.meal_prompt_tick(now)
    pay(rt.db, "Kopitiam Bugis", now - timedelta(minutes=30))
    assert rt.meal_prompt_tick(now)
    text, buttons = bot.sent[-1]
    assert text.startswith("🍽 Dinner at Kopitiam Bugis?") and "What did you have?" in text
    assert buttons == [[("✖ Skip", "fskip:0")]]
    reply, food_buttons = rt.phone.answer("chicken rice and kopi siew dai", now + timedelta(minutes=5))
    assert reply.startswith("🍽 Here's what I worked out:") and food_buttons[0][0][1].startswith("flog:")


def test_a_payment_processor_isnt_named_as_the_place(settings):
    rt, bot = nudge_runtime(settings)
    now = datetime.now(SG).replace(hour=8, minute=20, second=0, microsecond=0)
    rt.meal_prompt_tick(now)
    pay(rt.db, "STRIPE PAYMENTS SINGAPORE PTE. LTD.", now - timedelta(minutes=18), "2.92")
    rt.meal_prompt_tick(now)
    assert bot.sent[-1][0].startswith("🍽 Breakfast? (SGD 2.92 at 8:02am)\nWhat did you have?")


def test_a_question_during_the_meal_question_is_still_answered(settings):
    rt, bot = nudge_runtime(settings)
    now = datetime.now(SG).replace(hour=19, minute=30, second=0, microsecond=0)
    rt.meal_prompt_tick(now)
    pay(rt.db, "Kopitiam Bugis", now - timedelta(minutes=30))
    rt.meal_prompt_tick(now)
    assert not rt.phone.answer("how am i doing today?", now)[0].startswith("🍽 Here's what I worked out")


def test_something_else_and_skip(settings):
    rt, bot = nudge_runtime(settings)
    now = datetime.now(SG).replace(hour=12, minute=30, second=0, microsecond=0)
    before = now - timedelta(days=2)
    pay(rt.db, "Kopitiam", before)
    logged_at(rt.brain.fit, before + timedelta(minutes=10))
    rt.meal_prompt_tick(now)
    pay(rt.db, "Kopitiam", now - timedelta(minutes=10))
    rt.meal_prompt_tick(now)
    buttons = bot.sent[-1][1]
    assert rt.button(buttons[1][0][1]).startswith("✏️ What did you have?")
    assert rt.brain.fit.batch(buttons[0][0][1].split(":")[1])[0]["status"] == "cancelled"  # the draft went
    assert rt.phone.answer("laksa", now)[0].startswith("🍽 Here's what I worked out")
    assert rt.button("fskip:0") == "👍 Okay, not logging that one."


@pytest.mark.parametrize("change", [{"refund": 1}, {"category": "groceries"}, {"hours": 6}, {"hour": 2}, {"logged": True}])
def test_no_meal_question_when_it_isnt_worth_asking(settings, change):
    rt, bot = nudge_runtime(settings)
    now = datetime.now(SG).replace(hour=change.get("hour", 13), minute=30, second=0, microsecond=0)
    rt.meal_prompt_tick(now)
    if change.get("logged"):
        logged_at(rt.brain.fit, now - timedelta(minutes=5))
    pay(rt.db, "Kopitiam", now - timedelta(hours=change.get("hours", 0), minutes=10),
        category=change.get("category", "dining"), refund=change.get("refund", 0))
    assert not rt.meal_prompt_tick(now) and not bot.sent


def test_your_usual_breakfast_comes_at_930(settings):
    rt, bot = nudge_runtime(settings)
    today = datetime.now(SG).replace(hour=9, minute=35, second=0, microsecond=0)
    for back in (1, 2, 4):
        logged_at(rt.brain.fit, today - timedelta(days=back, minutes=60), "Kaya toast set", 480, 18)
    assert rt.nudge_tick(today.replace(hour=9, minute=0)) == []
    assert rt.nudge_tick(today) == ["breakfast"]
    text, buttons = bot.sent[-1]
    assert text == "☀️ Kaya toast set again this morning? (480 kcal, 18 g protein)"
    rt.button(buttons[0][0][1])
    assert rt.brain.fit.totals(today.date()).kcal == 480
    assert rt.nudge_tick(today + timedelta(minutes=5)) == []  # once a day


def test_no_breakfast_question_without_a_habit(settings):
    rt, bot = nudge_runtime(settings)
    today = datetime.now(SG).replace(hour=9, minute=35, second=0, microsecond=0)
    for back in (1, 9):  # twice isn't a habit
        logged_at(rt.brain.fit, today - timedelta(days=back, minutes=60), "Kaya toast set", 480, 18)
    assert rt.nudge_tick(today) == []


def test_a_heads_up_before_dinner_out(settings):
    rt, bot = nudge_runtime(settings)
    fit = rt.brain.fit
    fit.set_targets(kcal=2000, protein=120)
    eat(fit, fit.today(), 900, 50)
    today = datetime.now(SG).replace(hour=15, minute=5, second=0, microsecond=0)
    day = today.date().isoformat()
    rt.brain.calendar_events = lambda first, last: ([
        {"title": "Dinner with Alex", "first": day, "last": day, "start": "19:30", "location": "Jumbo Seafood"},
        {"title": "Team meeting", "first": day, "last": day, "start": "16:00"}], "")
    assert rt.nudge_tick(today) == ["meal out: Dinner with Alex"]
    text = bot.sent[-1][0]
    assert text.startswith("🍽 Dinner with Alex at 7:30pm today (Jumbo Seafood). So far today: 900 kcal, 50 g protein.")
    assert "about 1,100 kcal for the rest of the day, so keep things light before dinner" in text
    assert rt.db.last_message("fitness")["text"] == text


def test_the_weekly_review_offers_target_changes(settings):
    rt, bot = nudge_runtime(settings)
    fit = rt.brain.fit
    fit.set_targets(kcal=2000, protein=140)
    monday = fit.today() - timedelta(days=fit.today().weekday())
    for back in range(1, 15):  # two weeks at about 100 g protein
        eat(fit, monday - timedelta(days=back), 1950, 100)
    now = datetime.combine(monday, datetime.min.time(), SG).replace(hour=9, minute=5)
    assert "weekly" not in rt.nudge_tick(now.replace(hour=8))
    assert "weekly" in rt.nudge_tick(now)
    review, offer = bot.sent[-2], bot.sent[-1]
    assert review[0].startswith("📅 Your week: Last week") and "protein 40 g a day short" in review[0]
    assert "Protein has been short for a while: 100 g a day last week" in offer[0]
    assert offer[1] == [[("✅ Set 100 g", "fgoal:protein:100"), ("Keep 140 g", "fkeep:protein")]]
    assert rt.button("fgoal:protein:100") == "✅ Done: 100 g protein a day."
    assert fit.targets()["protein"] == 100 and rt.button("fkeep:protein") == "👍 Keeping your protein target as it is."
    assert "weekly" not in rt.nudge_tick(now + timedelta(hours=1))  # once a week


def test_the_weekly_offer_is_a_save_form_in_the_app(settings):
    client = chat_client(settings)
    rt = client.app.state.runtime
    fit = rt.brain.fit
    fit.set_targets(kcal=2000, protein=140)
    monday = fit.today() - timedelta(days=fit.today().weekday())
    for back in range(1, 15):
        eat(fit, monday - timedelta(days=back), 1950, 100)
    assert rt.weekly_review(datetime.now(SG))
    page = client.get("/chat/fitness").text
    assert "📅 Your week: Last week" in page and "Change anything and press Save" in page
    assert 'name="protein" inputmode="numeric" value="100"' in page and 'name="kcal" inputmode="numeric" value="2000"' in page


def test_no_target_changes_after_one_odd_week(settings):
    rt, bot = nudge_runtime(settings)
    fit = rt.brain.fit
    fit.set_targets(kcal=2000, protein=140)
    monday = fit.today() - timedelta(days=fit.today().weekday())
    for back in range(1, 29):
        eat(fit, monday - timedelta(days=back), 1950, 100 if back <= 7 else 150)
    assert nudges.proposals(fit) == []
