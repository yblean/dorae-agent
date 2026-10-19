"""Dorae-3: targets, logging food, steps from Apple Health, the 6pm check, its chat and Telegram."""
import dataclasses
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from doraemon import coach
from doraemon.db import Database
from doraemon.fitness import Fitness, import_steps, parse_steps, suggest_targets
from doraemon.foodlog import amount_of, lookup_product, resolve, understand
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
])
def test_what_counts_as_food_to_log(text, own, expected):
    assert coach.food_request(text, own) == expected


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
    assert "Total: 710 kcal, 27 g protein" in text and buttons[0][0][0] == "✅ Log"
    assert rt.button(buttons[0][0][1]).startswith("✅ Logged. Today: 710 kcal")
    assert rt.button(buttons[0][0][1]) == "That one's already logged (or was cancelled)."
    assert rt.phone.answer("how am i doing today?")[0].startswith("Today, ")


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
    assert rt.button(buttons[0][1][1]) == "✖ Not logged."
