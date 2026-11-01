"""Recognises the everyday questions, so they're answered straight from your data without the model.

Asking the local model takes 10-20 seconds (a topic check, a tool call, then the wording). Most
questions are plain lookups: "how much did I spend this week", "what needs my OK", "my reminders".
Looking those up takes milliseconds, so they skip the model. Everything else (advice, "why",
comparisons, odd wording) still goes to the model.

Precision over coverage: a question takes the fast path only if the WHOLE of it matches one of
the shapes below, apart from greetings and "please". One extra word ("why", "should", "compared
to") and it goes to the model, which reads it properly.

    route("money", "how much did I spend on dining last month?")
    -> Route(agent="money", intent="total", slots={"category": "dining", "period": "last_month"})
"""
import re
from dataclasses import dataclass, field

from doraemon.dates import unslang
from doraemon.schema import Category

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MONTH = "|".join(m for name in MONTHS for m in (name, name[:3])) + "|sept"
# Spoken period -> Brain.PERIODS name; months become "month:<n>"
_PERIOD = (r"(?:(?:so far )?today|yesterday|(?:so far )?this week|last week|previous week|the past week|"
           r"(?:in |over )?the (?:last|past) 7 days|(?:so far )?this month|last month|previous month|"
           rf"(?:in |for |during |on |from |over )?(?:the month of )?(?:{_MONTH}))")
CATEGORY_WORDS = {
    **{c.value: c.value for c in Category}, "food": "dining", "eating out": "dining", "restaurants": "dining",
    "meals": "dining", "grocery": "groceries", "supermarket": "groceries", "transportation": "transport",
    "rides": "transport", "taxis": "transport", "subscription": "subscriptions",
    "membership": "memberships", "bills": "utilities", "fun": "entertainment", "medical": "health",
    "travel": "travel", "flights": "travel", "hotels": "travel", "shops": "shopping",
}
_CAT = "|".join(sorted((re.escape(w) for w in CATEGORY_WORDS), key=len, reverse=True))
_POLITE_START = (r"(?:(?:hey|hi|hello|ok|okay|so|um)[ ,]+)*(?:dorae-?[12][ ,]+)?(?:(?:can|could|would) you |please |pls )?"
                 r"(?:(?:tell|show|give|get) me |let me (?:see|know) |list |check |show |find )?")
_POLITE_END = r"(?:[ ,]+(?:please|pls|thanks|thank you|thx|ty|again|now))*"
_P, _C = rf"(?P<period>{_PERIOD})", rf"(?P<category>{_CAT})"
_SPEND = r"(?:spend|spent|spending|pay|paid|pay out)"
# "did I", "have I", "I have", "I've", "I", or nothing: "how much (have I / I have / I've) spent"
_I = r"(?:(?:did|have|do|had) i |i(?: have|'ve| had| did)? )?"
_MY = r"(?:my |the |all my |all the |all )?"
_THING = r"(?:purchase|payment|transaction|spend|thing i (?:bought|paid for)|buy)"

PATTERNS: dict[str, list[tuple[str, str]]] = {  # agent -> [(intent, pattern)], first match wins
    "money": [
        ("total", rf"(?:how much|what) (?:money )?{_I}{_SPEND}(?: in total| altogether| total)?"
                  rf"(?: (?:on|for) {_C}| at (?P<merchant>[a-z0-9][\w&' .-]{{1,30}}?))?(?: {_P})?"),
        ("total", rf"(?:how much|what) (?:is|was|did) (?:my|the) (?:total )?(?:{_C} )?spending(?: come to| total)?(?: {_P})?"),
        ("total", rf"{_MY}(?:total )?(?:{_C} )?spending(?: (?:on|for) {_C})?(?: {_P})"),
        ("breakdown", rf"{_MY}(?:(?:{_P})(?:'s)? )?(?:spending |monthly )?breakdown(?: (?:for |of )?{_P})?"),
        ("breakdown", rf"{_MY}spending by category(?: {_P})?"),
        ("latest", rf"(?:what(?:'s| is| was) )?{_MY}(?:latest|last|most recent|newest) (?:{_THING}|purchases|payments|transactions)"),
        ("latest", rf"{_MY}recent (?:purchases|payments|transactions|spending)"),
        ("biggest", rf"(?:what(?:'s| is| was) )?{_MY}(?:biggest|largest|most expensive) {_THING}(?: {_P})?"),
        ("top", rf"where (?:did|have) i (?:{_SPEND}|spent) (?:the )?most(?: money)?(?: {_P})?"),
        ("top", rf"{_MY}top (?:merchants|shops|places|stores)(?: {_P})?"),
        ("payments", rf"{_MY}{_C} (?:payments|purchases|transactions)(?: {_P})?"),
        ("payments", rf"{_MY}(?:payments|purchases|transactions)(?: (?:on|for) {_C})?(?: {_P})?"),
        ("how_was", rf"how (?:was|did) {_P}(?: go| look)?"),
        ("budget", r"how am i doing (?:on|with) (?:my )?budgets?|(?:my )?budgets?(?: status)?|"
                   r"am i (?:over|within|under|on track with|on track for) (?:my )?budgets?|"
                   r"how(?:'s| is) my budget(?: doing| going| looking)?"),
        ("networth", r"(?:what(?:'s| is) )?(?:my )?(?:total )?net ?worth(?: now| today| (?:this|last) month)?|how much am i worth|"
                     r"how(?:'s| is| did| has) my net ?worth(?: (?:doing|going|changed?|looking))?(?: (?:this month|since last month))?|"
                     r"(?:my )?net ?worth (?:change|trend|this month|mom|month on month)"),
        ("income", rf"how much (?:did|have) i (?:earn|earned|make|made|receive|received|get paid)(?: in total)?(?: {_P})?|"
                   rf"(?:what(?:'s| is| was) )?{_MY}income(?: {_P})?|how much (?:did|have) i (?:save|saved)(?: {_P})?|"
                   rf"(?:what(?:'s| is| was) )?(?:my )?sav(?:ings|ing) rate(?: {_P})?"),
        ("holdings", r"(?:what are |show |list )?(?:my )?(?:investments|holdings|portfolio|positions|stocks)|what do i (?:hold|own)"),
        ("trend", rf"is my (?:{_C} )?spending going (?:up|down)(?: or (?:up|down))?|{_MY}(?:monthly )?(?:{_C} )?spending trend|"
                  rf"{_MY}(?:{_C} )?spending (?:by|per|each|every) month|{_MY}monthly spending"),
    ],
    "calendar": [
        ("waiting", r"what needs my (?:ok|okay|approval|attention|confirmation)|what(?:'s| is) (?:waiting|pending)"
                    r"(?: for (?:my )?(?:ok|okay|approval))?|(?:anything|what) (?:to|for me to) (?:confirm|approve)|"
                    r"(?:my )?pending (?:items|stuff)"),
        ("week", r"(?:what(?:'s| is) (?:on )?)?my (?:schedule|calendar|week|agenda)(?: (?:for )?(?P<which>this|next) week)?|"
                 r"what(?:'s| is) (?:on |happening |planned |coming up )?(?:for )?(?P<which>this|next) week|"
                 r"(?P<which>this|next) week'?s? (?:schedule|plans|events|agenda)|"
                 r"what do i have(?: going on| on| planned| coming up)?(?: for)? (?P<which>this|next) week|"
                 r"what am i (?:doing|up to) (?P<which>this|next) week|am i (?:busy|free) (?P<which>this|next) week|"
                 r"(?:any|my|all my) (?:events|plans|appointments|meetings)(?: for)? (?P<which>this|next) week|"
                 r"how(?:'s| is| does) my week(?: looking| going| look)?|how(?:'s| is| does) (?P<which>next) week(?: looking| look)?|"
                 r"(?:and |what about |how about |ok and |and for )?(?:the )?(?P<which>this|next) week"),
        ("day", r"what(?:'s| is) (?:on|happening|due|planned)(?: for me)? (?P<day>today|tomorrow)|"
                r"(?:anything|what do i have) (?:on |due |planned )?(?P<day>today|tomorrow)|"
                r"my (?:schedule|agenda|plans|day) (?:for )?(?P<day>today|tomorrow)"),
        ("due", r"what(?:'s| is) (?:due|coming up)(?: (?:this week|soon|in the next (?:7|seven) days))?|"
                r"(?:any |my )?(?:upcoming )?bills(?: due)?(?: (?:this week|soon))?"),
        ("trips", r"(?:my |any )?(?:upcoming )?(?:trips|travel|flights)(?: coming up)?|"
                  r"do i have any (?:trips|flights|travel)(?: coming up| planned| soon)?"),
        ("reminders", r"(?:what are )?my (?:current |upcoming |active )?reminders|what reminders do i have|"
                      r"(?:current|upcoming|active|all) reminders"),
        ("passed", r"what (?:has )?(?:already )?passed|what did i miss|(?:the )?ones? that passed"),
    ],
    "fitness": [  # Dorae-3 (doraemon.coach answers them)
        ("fit_week", r"(?:my )?(?:calories|kcal|protein|steps|macros)(?: and (?:calories|kcal|protein|steps))? "
                     r"(?:(?:for |over )?(?:this|last|the past|the last) week|(?:over |for )?(?:the )?(?:last|past) 7 days)|"
                     r"how(?:'s| is| was) my week (?:for|of) (?:calories|protein|steps|eating|food)|"
                     r"my (?:weekly|week'?s) (?:calories|protein|steps|progress|macros)"),
        ("fit_day", r"(?:my )?(?:calories|kcal|protein|macros|steps)(?: and (?:calories|kcal|protein|steps|macros))*"
                    r"(?: (?:for |so far )?(?P<fday>today|yesterday))?(?: so far)?|"
                    r"how many (?:calories|kcal|steps)(?: have i| did i| i)?(?: had| eaten| eat| done| taken| walked| got)?"
                    r"(?: (?P<fday>today|yesterday))?(?: so far)?|"
                    r"how much protein(?: have i| did i| i)?(?: had| eaten| eat| got)?(?: (?P<fday>today|yesterday))?(?: so far)?|"
                    r"(?:how much )?(?:protein|calories|kcal|steps) (?:left|to go|remaining)(?: today| for today)?|"
                    r"what (?:did|have) i (?:eat|eaten|had)(?: (?P<fday>today|yesterday))?"),
        ("fit_targets", r"(?:what are )?my (?:daily )?(?:targets|goals)|(?:what(?:'s| is) )?my (?:calorie|protein|step|steps) "
                        r"(?:target|goal)"),
    ],
}
# Only in Dorae-3's own chat: "how am I doing?" in Dorae-1's chat is about money
OWN_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "fitness": [("fit_day", r"how am i doing(?: (?P<fday>today|yesterday))?(?: so far)?|am i on track(?: today)?|"
                            r"how(?:'s| is| was) (?:my )?(?P<fday>today|yesterday)(?: going)?|(?P<fday>today|yesterday)"),
                ("fit_week", r"how(?:'s| is| was) my week|(?:this|my) week|am i on track this week")],
}


def _numbered(pattern: str) -> str:
    """Python regexes can't reuse a group name, so repeats become period2, period3... (read back as `period`)."""
    seen: dict[str, int] = {}

    def rename(m: re.Match) -> str:
        seen[m[1]] = seen.get(m[1], 0) + 1
        return f"(?P<{m[1]}{seen[m[1]] if seen[m[1]] > 1 else ''}>"
    return re.sub(r"\(\?P<([a-z]+)>", rename, pattern)


COMPILED = {agent: [(intent, re.compile(rf"{_POLITE_START}(?:{_numbered(pattern)}){_POLITE_END}"))
                    for intent, pattern in patterns] for agent, patterns in PATTERNS.items()}
COMPILED_OWN = {agent: [(intent, re.compile(rf"{_POLITE_START}(?:{_numbered(pattern)}){_POLITE_END}"))
                        for intent, pattern in patterns] for agent, patterns in OWN_PATTERNS.items()}
FOLLOW_UP = re.compile(rf"(?:and|what about|how about|ok and|and for|same for)? ?{_P}")
TAKES_PERIOD = {"total", "breakdown", "biggest", "top", "payments", "how_was", "income"}


@dataclass
class Route:
    agent: str          # whose question it is (may differ from the chat it was asked in)
    intent: str
    slots: dict = field(default_factory=dict)


def clean(question: str) -> str:
    q = unslang(question).lower().replace("’", "'")
    q = re.sub(r"[?!]+|(?<!\d)\.|\.(?!\d)", " ", q)  # keeps the point in 4.87
    q = re.sub(r"\s+", " ", q).strip(" ,")
    return q


def period_name(text: str | None) -> str | None:
    """'this week' -> 'this_week', 'in sept' -> 'month:9'. None when there's no period."""
    if not text:
        return None
    text = re.sub(r"^(?:so far |in |for |during |over |on |from |the month of )+|^the ", "", text.strip())
    for name, words in (("today", "today"), ("yesterday", "yesterday"), ("this_week", "this week"),
                        ("last_week", "last week|previous week|past week"), ("last_7_days", "7 days"),
                        ("this_month", "this month"), ("last_month", "last month|previous month")):
        if re.search(words, text):
            return name
    month = next((i for i, m in enumerate(MONTHS, 1) if text.startswith(m[:3])), None)
    return f"month:{month}" if month else None


def _match(agent: str, q: str, own: bool = False) -> Route | None:
    """`own`: also the shapes that only count in this agent's own chat (OWN_PATTERNS)."""
    for intent, pattern in COMPILED[agent] + (COMPILED_OWN.get(agent, []) if own else []):
        m = pattern.fullmatch(q)
        if m is None:
            continue
        g = {}
        for k, v in m.groupdict().items():
            if v:
                g.setdefault(k.rstrip("0123456789"), v)
        slots = {}
        if "period" in g:
            slots["period"] = period_name(g["period"])
        if "category" in g:
            slots["category"] = CATEGORY_WORDS[g["category"]]
        if "merchant" in g:
            slots["merchant"] = g["merchant"].strip()
        for key in ("which", "day"):
            if key in g:
                slots[key] = g[key]
        if "fday" in g:
            slots["day"] = g["fday"]
        if agent == "money" and "merchant" in slots and slots["merchant"] in CATEGORY_WORDS:
            slots["category"] = CATEGORY_WORDS[slots.pop("merchant")]  # "at grab" reads as transport, not a shop
        return Route(agent, intent, slots)
    return None


def route(agent: str, question: str, previous: str | None = None) -> Route | None:
    """The fast-path reading of `question` asked in `agent`'s chat, or None to ask the model.
    `previous`: your last question in this chat, for follow-ups like "and last month?"."""
    q = clean(question)
    if not q or len(q) > 120:
        return None
    found = _match(agent, q, own=True)
    if found:
        return found
    # a follow-up of your last question ("and this week?") comes before the other agents' reading of it
    if previous and (m := re.fullmatch(rf"{_POLITE_START}{FOLLOW_UP.pattern}{_POLITE_END}", q)):
        before = route(agent, previous)
        if before and before.agent == agent and before.intent in TAKES_PERIOD:
            return Route(agent, before.intent, {**before.slots, "period": period_name(m["period"])})
    # another agent's question (the caller points you there), or else the looser reading below
    for other in (a for a in PATTERNS if a != agent):
        if found := _match(other, q):
            return found
    return understand(agent, q, previous)


# --- the looser reading: topic words and time words anywhere in the question ------------------------
#
# The shapes above take a question only if ALL of it matches. Most lookups are worded more freely
# ("whats my schedule like next week", "do i have anything on november", "did i have a 4.87 transaction
# today"), so understand() looks for what it's about (spending, budget, schedule, reminders, bills,
# trips) and when (today, friday, this weekend, november) anywhere in it, after fixing common typos.
# Anything that needs thought (why, should, how can I, compare, advice) or asks to change something
# (add, set, cancel) still goes to the model: those are the questions it's for.

TYPOS = {"monrh": "month", "mnth": "month", "mth": "month", "montn": "month", "wk": "week", "wkend": "weekend",
         "weeekend": "weekend", "whats": "what's", "wats": "what's", "spendings": "spending", "spendng": "spending",
         "calender": "calendar", "calandar": "calendar", "calander": "calendar", "schedual": "schedule",
         "shedule": "schedule", "schdule": "schedule", "remindar": "reminder", "remindars": "reminders",
         "reminer": "reminder", "reminers": "reminders", "transcation": "transaction", "transcations": "transactions",
         "payemnts": "payments", "budjet": "budget", "buget": "budget", "tmrw": "tomorrow", "anythin": "anything"}
# Words worth correcting when one letter is off ("spemt", "novemebr"); short words are left alone
VOCAB = ["month", "week", "weekend", "spend", "spent", "spending", "budget", "budgets", "schedule", "calendar",
         "reminder", "reminders", "payment", "payments", "transaction", "transactions", "purchase", "purchases",
         "yesterday", "tomorrow", "today", "anything", "events", "appointments", "deadlines", "flights",
         "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", *MONTHS,
         "monthly", "weekly", "daily", "weekends", "months", "weeks", "events"]  # real words, so they stay as they are

REAL_WORDS = {"remind", "mouth", "money", "spends", "events", "minute", "mention", "monster", "mother", "weeks",
              "spent", "payment", "purchase", "today", "fridays", "sunday", "monday", "transport"}
NEEDS_THOUGHT = re.compile(
    r"\b(?:why|should|shall|how (?:can|could|do|should|would|to)|could i|can i afford|afford|advice|advise|tips?|"
    r"suggest|recommend|cut (?:down|back)|reduce|save more|compare[ds]?|comparison|versus|vs|better|worse|predict|"
    r"forecast|will i|if i|what if|explain|ideas?|joke|poem|write|story|translate)\b")
CHANGES = re.compile(r"^(?:\W*(?:hey|hi|ok|okay|so|please|pls|can you|could you|help me(?: to)?)\W+)*"
                     r"(?:add|create|make|set|book|put|schedule (?:a|an|my|me)|remind|cancel|delete|remove|move|"
                     r"change|edit|update|rename|mark)\b|\bremind me\b")
MONEY_TOPIC = re.compile(r"\b(?:spend|spent|spending|paid|pay|payments?|costs?|purchases?|bought|buy|transactions?|"
                         r"expenses?|money|charged?|budgets?|net ?worth|worth|income|salary|earn(?:ed|t)?|"
                         r"holdings?|portfolio|investments?|stocks?|merchants?|shops?)\b")
CALENDAR_TOPIC = re.compile(r"\b(?:schedule|calendar|agenda|events?|plans?|planned|busy|free|anything|"
                            r"what do i have|what(?:'s| is) on|happening|appointments?|meetings?|reminders?|bills?|"
                            r"due|deadlines?|trips?|flights?|travel|hotels?|needs? my (?:ok|okay|approval)|pending|"
                            r"waiting for my|to (?:confirm|approve))\b")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_WORD = r"(?:mon|tue|tues|wed|wednes|thu|thur|thurs|fri|sat|satur|sun)(?:day)?"
# "when is my dentist appointment", "what time is the concert", "find my flight to tokyo": a search for one thing
FIND = re.compile(r"^(?:(?:hey|hi|ok|so|please|pls|can you|could you)\W+)*(?:when(?:'s| is| are| will)|"
                  r"what (?:time|day|date) is|find|look up|search(?: for)?)\s+(?:my |the |a |an |our )?(?P<what>.+)$")
# ...unless it's one of the lists the other intents answer ("when is my next flight": the trips)
TOPIC_ONLY = (r"(?:next |upcoming )?(?:flights?|trips?|travel|bills?(?: due)?|reminders?|payments?|budgets?|"
              r"schedule|calendar|agenda)(?: coming up)?")
SUMMARY = re.compile(r"\b(?:monthly )?summary\b|\bin review\b|\breview of\b|\bwrap[- ]?up\b|\brecap\b")
HELP = re.compile(r"^(?:\W*(?:hey|hi|ok|so)\W+)*(?:help|what can (?:you|dorae-?[12]|doraemon) do|"
                  r"what (?:can|do) i ask(?: you)?|how do i use (?:you|this))\W*$")


def fix_typos(q: str) -> str:
    import difflib
    words = []
    for w in q.split(" "):
        fixed = TYPOS.get(w, w)
        # a real word that starts a vocabulary word ("remind" of reminder) is left alone: it's not a typo
        if fixed == w and len(w) >= 5 and w.isalpha() and w not in VOCAB and w not in REAL_WORDS \
                and not any(v.startswith(w) or w.startswith(v) for v in VOCAB):
            close = difflib.get_close_matches(w, VOCAB, n=1, cutoff=0.8)
            fixed = close[0] if close else w
        words.append(fixed)
    return " ".join(words)


def spoken_when(q: str) -> str | None:
    """The time a calendar question is about, as Brain.days_range reads it: 'today', 'tomorrow',
    'this_weekend', 'next_weekend', 'this_week', 'next_week', 'last_week', 'this_month', 'next_month', 'month:11',
    'weekday:4' (+ ':next'), 'date:26/10'. None when it names no time."""
    for name, words in (("today", r"\btoday\b"), ("tomorrow", r"\btomorrow\b"),
                        ("next_weekend", r"\bnext weekend\b"), ("this_weekend", r"\b(?:this |the )?weekend\b"),
                        ("next_week", r"\bnext week\b"), ("last_week", r"\b(?:last|previous|past) week\b"),
                        ("this_week", r"\b(?:this|the|my) week\b"), ("next_month", r"\bnext month\b"),
                        ("this_month", r"\bthis month\b")):
        if re.search(words, q):
            return name
    if m := re.search(rf"\b(?:(this|next|coming) )?({_DAY_WORD})\b", q):
        day = next(i for i, d in enumerate(_WEEKDAYS) if d.startswith(m[2][:3]))
        return f"weekday:{day}" + (":next" if m[1] == "next" else "")
    if m := re.search(r"\b(\d{1,2})[/.](\d{1,2})\b", q):
        return f"date:{m[1]}/{m[2]}"
    if m := re.search(rf"\b(?:{_MONTH})\b(?! (?:i|you|we)\b)", q):  # not "may i see..."
        return f"month:{next(i for i, n in enumerate(MONTHS, 1) if m[0].startswith(n[:3]))}"
    return None


def loose_period(q: str) -> str | None:
    """A spending period named anywhere in the question ('so far this month', 'in sept', 'yesterday')."""
    if m := re.search(rf"\b(?:{_PERIOD})\b", q):
        return period_name(m[0])
    return None


def understand(agent: str, q: str, previous: str | None = None) -> Route | None:
    """The looser reading of an everyday lookup, or None for the model."""
    q = fix_typos(q)
    if HELP.match(q):
        return Route(agent, "help")
    if review := fit_review(agent, q):  # "how can I improve" too: the review's tips are worked out, not written
        return review
    if NEEDS_THOUGHT.search(q) or CHANGES.search(q):
        return None
    if (found := FIND.match(q)) and not re.fullmatch(TOPIC_ONLY, found["what"]):
        return Route("calendar", "find", {"what": found["what"].strip()})  # "when is my dentist appointment"
    if SUMMARY.search(q):
        return Route("money", "summary", {"period": loose_period(q)} if loose_period(q) else {})
    if FITNESS_TOPIC.search(q) and not re.search(r"\b(?:spend|spent|spending|paid|pay|cost|budgets?|sgd)\b|\$", q):
        return _fitness(q)
    money, calendar = bool(MONEY_TOPIC.search(q)), bool(CALENDAR_TOPIC.search(q))
    if money and calendar:  # "bills" and "spend" both: read the stronger signals
        money = not re.search(r"\b(?:bills?|due|deadlines?|reminders?|schedule|calendar)\b", q)
        calendar = not money
    if not (money or calendar):
        # a bare follow-up of your last question: "what about friday?", "and in september?"
        if previous and len(q.split()) <= 6:
            before = route(agent, previous)
            if before and before.intent in ("days", "week", "day") and spoken_when(q):
                return _calendar_route(spoken_when(q), "days")
            if before and before.intent in TAKES_PERIOD and loose_period(q):
                return Route(before.agent, before.intent, {**before.slots, "period": loose_period(q)})
        return None
    return _money(q) if money else _calendar(q)


NOT_A_SHOP = {"my", "the", "a", "an", "this", "last", "next", "today", "yesterday", "tomorrow", "it", "them", "that",
              "stuff", "things", "average", "total", "budget", "budgets", "spending", "payments", "card", "paylah",
              "food", "me", "earth", "track", "top", *_WEEKDAYS, *(d[:3] for d in _WEEKDAYS), *CATEGORY_WORDS}


def _money(q: str) -> Route | None:
    slots = {}
    if period := loose_period(q):
        slots["period"] = period
    cat = re.search(rf"\b(?:on|for) (?P<c>{_CAT})\b|\b(?P<c2>{_CAT}) (?:spending|payments?|purchases?|transactions?)\b", q)
    if cat:
        slots["category"] = CATEGORY_WORDS[cat["c"] or cat["c2"]]
    if (shop := re.search(r"\bat ([a-z0-9][\w&' .-]{1,30}?)(?: (?:on|in|this|last|today|yesterday|so far)\b|$)", q)) \
            and not re.match(r"\d", shop[1]):
        name = shop[1].strip()
        if re.match(r"(?:the|my|a|an) ", name):  # "at the airport on my trip": a place, not a shop's name
            return None
        if name in CATEGORY_WORDS:
            slots["category"] = CATEGORY_WORDS[name]
        else:
            slots["merchant"] = name
    if not ({"merchant", "category"} & slots.keys()):  # "how much did grab cost me", "spent on netflix"
        shop = re.search(r"\bdid ([a-z0-9][\w&'-]{1,25}) cost\b|\b(?:on|at|from|with) ([a-z][\w&'-]{1,25})\b", q)
        name = shop and (shop[1] or shop[2])
        if name and name not in NOT_A_SHOP and not name.startswith(tuple(m[:3] for m in MONTHS)):
            slots["merchant"] = name
    if re.search(r"\bnet ?worth\b|\bhow much am i worth\b", q):
        return Route("money", "networth")
    if re.search(r"\b(?:income|salary|earn(?:ed|t)?|got paid|kept|savings?|saved)\b", q):
        return Route("money", "income", {k: v for k, v in slots.items() if k == "period"})
    if re.search(r"\b(?:holdings?|portfolio|investments?|stocks?|positions)\b", q):
        return Route("money", "holdings")
    if re.search(r"\bbudgets?\b", q):
        return Route("money", "budget")
    if re.search(r"\b(?:trend|each month|per month|by month|monthly|going (?:up|down)|over time|over the months)\b", q):
        return Route("money", "trend", {k: v for k, v in slots.items() if k == "category"})
    if re.search(r"\b(?:breakdown|by category|categor(?:y|ies)|split)\b", q):
        return Route("money", "breakdown", {k: v for k, v in slots.items() if k == "period"})
    if re.search(r"\b(?:top|most)\b.*\b(?:merchants?|shops?|places?|stores?)\b|\bwhere\b.*\bmost\b", q):
        return Route("money", "top", {k: v for k, v in slots.items() if k == "period"})
    if re.search(r"\b(?:biggest|largest|most expensive|highest|priciest)\b", q):
        return Route("money", "biggest", {k: v for k, v in slots.items() if k == "period"})
    if re.search(r"\b(?:latest|newest|most recent|recent|recently|lately|last (?:purchase|payment|transaction|thing))\b", q):
        return Route("money", "latest")
    if amount := re.search(r"(?<![\d/])\$?(\d+\.\d{1,2})\b", q):
        return Route("money", "payments", {**slots, "amount": amount[1]})
    if re.search(r"\b(?:payments?|transactions?|purchases?|list|show|what did i (?:buy|spend (?:it )?on|pay for)|"
                 r"what i (?:bought|spent on))\b", q):
        return Route("money", "payments", slots)
    return Route("money", "total", slots)


def _calendar(q: str) -> Route | None:
    if re.search(r"\breminders?\b", q):
        return Route("calendar", "reminders")
    if re.search(r"\bneeds? my (?:ok|okay|approval)\b|\bpending\b|\bwaiting for my\b|\bto (?:confirm|approve)\b", q):
        return Route("calendar", "waiting")
    if re.search(r"\b(?:trips?|flights?|travel|hotels?)\b", q):
        return Route("calendar", "trips")
    when = spoken_when(q)
    if re.search(r"\b(?:bills?|due|deadlines?)\b", q):
        # the next 7 days; "due this month" or "the bill from SP" is a search for the model
        return Route("calendar", "due") if when in (None, "this_week") and not re.search(r"\b(?:from|for)\b", q) else None
    if when is None and not re.search(r"\b(?:schedule|calendar|agenda|plans|events|anything|busy|free|"
                                      r"what do i have|happening)\b", q):
        return None  # "when is my dentist appointment": a search for one thing, the model's to find
    return _calendar_route(when or "this_week", "days")


FITNESS_TOPIC = re.compile(r"\b(?:calories|calorie|kcal|protein|carbs?|carbohydrates?|macros?|steps|walked|ate|eaten|"
                           r"what did i eat|diet)\b")


# "am i hitting my targets", "based on what i ate the past few weeks, what can i improve on", "how have i been doing
# this month": several weeks against your targets, with what to improve (coach.review_card)
FIT_REVIEW = re.compile(r"\b(?:hit(?:ting)?|meet(?:ing)?|met|reach(?:ing|ed)?|on track|improve(?:ments?)?|improving|"
                        r"work on|analy[sz]e|analysis|review|overall|progress|how (?:have|has) (?:i|my \w+) been|"
                        r"how (?:did|was|am|have) i (?:been )?(?:doing|going|done|do)|"
                        r"how(?:'s| is| was| has) my (?:eating|diet|food|nutrition)(?: been)?)\b")
_COUNT = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "couple of": 2, "couple": 2,
          "few": 4, "several": 4}


def fit_review(agent: str, q: str) -> Route | None:
    """A review of your eating and steps against your targets, over the span the question names (4 weeks unless it
    says otherwise); None when it isn't one. Today or yesterday on its own is a day's lookup, not a review."""
    if not FIT_REVIEW.search(q) or CHANGES.search(q) or re.search(r"\b(?:today|tonight|yesterday)\b", q):
        return None
    if not (agent == "fitness" or FITNESS_TOPIC.search(q) or re.search(r"\b(?:eat|eating|food|meals?)\b", q)) \
            or re.search(r"\b(?:spend|spent|spending|paid|pay|cost|budgets?|sgd|savings?|income)\b|\$", q):
        return None
    if m := re.search(rf"\b(?:past|last|previous|these|the)\s+(\d{{1,3}}|{'|'.join(_COUNT)})\s+(days?|weeks?|months?)\b", q):
        n = int(m[1]) if m[1].isdigit() else _COUNT[m[1]]
        return Route("fitness", "fit_review", {"span": f"days:{n * {'d': 1, 'w': 7, 'm': 30}[m[2][0]]}"})
    for words, span in ((r"last month|previous month", "month:last"), (r"this month", "month:this"),
                        (r"(?:last|previous) week", "week:last"), (r"this week", "week:this"),
                        (r"past week|fortnight", "days:7" if "fortnight" not in q else "days:14")):
        if re.search(rf"\b(?:{words})\b", q):
            return Route("fitness", "fit_review", {"span": span})
    return Route("fitness", "fit_review", {"span": "days:28"})


def _fitness(q: str) -> Route:
    """Dorae-3's lookups: a day (today, yesterday), the week, or your targets."""
    if re.search(r"\b(?:targets?|goals?)\b", q) and not FIT_REVIEW.search(q):
        return Route("fitness", "fit_targets")
    when = spoken_when(q)
    if when in ("this_week", "last_week") or re.search(r"\b(?:week|weekly|7 days)\b", q):
        return Route("fitness", "fit_week", {"which": "last" if when == "last_week" else "this"})
    return Route("fitness", "fit_day", {"day": "yesterday" if re.search(r"\byesterday\b", q) else "today"})


def _calendar_route(when: str, intent: str) -> Route:
    if when in ("this_week", "next_week"):
        return Route("calendar", "week", {"which": when.split("_")[0]})
    return Route("calendar", intent, {"when": when})
