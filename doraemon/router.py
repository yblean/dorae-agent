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
                 r"what(?:'s| is) (?:on |happening )?(?P<which>this|next) week|(?P<which>this|next) week'?s? schedule"),
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
FOLLOW_UP = re.compile(rf"(?:and|what about|how about|ok and|and for|same for)? ?{_P}")
TAKES_PERIOD = {"total", "breakdown", "biggest", "top", "payments", "how_was", "income"}


@dataclass
class Route:
    agent: str          # whose question it is (may differ from the chat it was asked in)
    intent: str
    slots: dict = field(default_factory=dict)


def clean(question: str) -> str:
    q = unslang(question).lower().replace("’", "'")
    q = re.sub(r"[?!.]+", " ", q)
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


def _match(agent: str, q: str) -> Route | None:
    for intent, pattern in COMPILED[agent]:
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
    found = _match(agent, q)
    if found:
        return found
    other = "calendar" if agent == "money" else "money"
    found = _match(other, q)
    if found:
        return found  # the other agent's question: the caller points you there
    if previous and (m := re.fullmatch(rf"{_POLITE_START}{FOLLOW_UP.pattern}{_POLITE_END}", q)):
        before = route(agent, previous)
        if before and before.agent == agent and before.intent in TAKES_PERIOD:
            return Route(agent, before.intent, {**before.slots, "period": period_name(m["period"])})
    return None
