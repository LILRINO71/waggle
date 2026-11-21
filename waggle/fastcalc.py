"""Arithmetic and unit conversions in plain code, for the PC voice's no-model tier (waggle/voice_fast.py).

A spoken sum or conversion is answered exactly, in microseconds, or not at all: every function returns None
when the words are not a clean sum or conversion, and the caller then takes the next tier. Nothing here guesses.

    calc("what's 15 percent of 80")            -> {"value": 12.0, "say": "15 percent of 80 is 12"}
    convert("how many feet in a mile")         -> {"value": 5280.0, "say": "1 mile is 5,280 feet"}
"""
import ast
import math
import operator
import re

# ---- number words ------------------------------------------------------------------------------------------

_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
_SCALE = {"hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9}
_NUM_WORDS = set(_UNITS) | set(_TENS) | set(_SCALE) | {"a", "and"}


def _spoken_number(tokens, i):
    """(value, next index) for a number said in words starting at tokens[i] ("two hundred fifty six"), or None."""
    total, current, used, j = 0, 0, False, i
    while j < len(tokens):
        t = tokens[j]
        if t in _UNITS and not (t == "oh" and not used):
            current += _UNITS[t]
        elif t in _TENS:
            current += _TENS[t]
        elif t == "hundred" and used:
            current = max(current, 1) * 100
        elif t in ("thousand", "million", "billion") and used:
            total += max(current, 1) * _SCALE[t]
            current = 0
        elif t == "and" and used and j + 1 < len(tokens) and (tokens[j + 1] in _UNITS or tokens[j + 1] in _TENS) \
                and j > i and tokens[j - 1] in ("hundred", "thousand", "million"):
            j += 1
            continue
        else:
            break
        used = True
        j += 1
    return (total + current, j) if used else None


# ---- tokens ------------------------------------------------------------------------------------------------

_STEM = re.compile(r"^(?:what(?:'s| is| are)|whats|calculate|compute|work out|figure out|how much (?:is|are)|"
                   r"tell me|give me|can you (?:tell me|calculate|work out)|do the maths? (?:on|for)|"
                   r"what do you get (?:for|from|if you (?:add|multiply|divide|subtract)))\s+", re.I)
_TAIL = re.compile(r"\s*(?:equals?|please|sir|waggle|\?)*\s*$", re.I)

_OPS = {"plus": "+", "added to": "+", "minus": "-", "less": "-", "subtract": "-", "times": "*", "multiplied by": "*",
        "x": "*", "divided by": "/", "over": "/", "into": "/", "to the power of": "**", "to the power": "**",
        "raised to": "**"}
_OP_PHRASES = sorted(_OPS, key=len, reverse=True)
_SAY = {"+": "plus", "-": "minus", "*": "times", "/": "divided by", "**": "to the power of"}


def _tokens(text):
    """The expression as a list of ("num", float) / ("op", "+") / ("paren", "(") ... or None when anything else is in it."""
    t = text.lower().replace("’", "'").replace("×", " x ").replace("÷", " / ").replace("−", "-")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)  # 1,200 -> 1200
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = re.sub(r"(\d)\s*x\s*(\d)", r"\1 x \2", t)
    t = re.sub(r"([()+\-*/^])", r" \1 ", t)
    t = t.replace("**", " ** ")
    t = re.sub(r"\bsquare root of\b", "sqrt of", t)
    t = re.sub(r"\bcube root of\b", "cbrt of", t)
    t = re.sub(r"\bpercent of\b", "pct_of", t)
    t = re.sub(r"\bpercent\b", "pct", t)
    words = t.split()
    out, i = [], 0
    while i < len(words):
        w = words[i]
        matched = False
        for phrase in _OP_PHRASES:
            p = phrase.split()
            if words[i:i + len(p)] == p:
                out.append(("op", _OPS[phrase]))
                i += len(p)
                matched = True
                break
        if matched:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", w):
            out.append(("num", float(w)))
        elif w in ("(", ")"):
            out.append(("paren", w))
        elif w in ("+", "-", "*", "/", "**"):
            out.append(("op", w))
        elif w == "^":
            out.append(("op", "**"))
        elif w in ("sqrt", "cbrt", "pct_of", "pct", "squared", "cubed", "factorial", "half", "double", "twice",
                   "triple", "of", "the"):
            out.append(("word", w))
        elif w == "point" and out and out[-1][0] == "num":
            out.append(("word", "point"))
        else:
            got = _spoken_number(words, i)
            if not got:
                return None
            out.append(("num", float(got[0])))
            i = got[1]
            continue
        i += 1
    return _decimals(out)


def _decimals(toks):
    """"three point five" -> 3.5: a number, the word point, then digits said one by one."""
    out, i = [], 0
    while i < len(toks):
        if toks[i] == ("word", "point") and out and out[-1][0] == "num":
            digits = ""
            j = i + 1
            while j < len(toks) and toks[j][0] == "num" and toks[j][1] in range(10):
                digits += str(int(toks[j][1]))
                j += 1
            if not digits:
                return None
            out[-1] = ("num", float(f"{int(out[-1][1])}.{digits}"))
            i = j
            continue
        out.append(toks[i])
        i += 1
    return out


# ---- evaluation --------------------------------------------------------------------------------------------

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > 64 or abs(a) > 1e9 or (a < 0 and b != int(b))):
            raise ValueError("power out of range")
        return _BIN[type(node.op)](a, b)
    raise ValueError("not arithmetic")


def _build(toks):
    """(python expression, spoken form) from the tokens, or None. Words like "percent of" are rewritten into
    plain arithmetic before the expression is parsed and evaluated by the restricted evaluator above."""
    py, say = [], []
    i = 0
    prev_value = False  # the previous token ends a value (a number or a closing bracket)

    def num(v):
        return repr(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)

    def sayn(v):
        return fmt(v)
    while i < len(toks):
        kind, v = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if kind == "num":
            if prev_value:
                return None
            if nxt == ("word", "pct_of"):
                py.append(f"({num(v)}/100)*")
                say.append(f"{sayn(v)} percent of")
                prev_value = False
                i += 2
                continue
            if nxt == ("word", "pct"):
                py.append(f"({num(v)}/100)")
                say.append(f"{sayn(v)} percent")
                prev_value, i = True, i + 2
                continue
            if nxt == ("word", "squared"):
                py.append(f"({num(v)}**2)")
                say.append(f"{sayn(v)} squared")
                prev_value, i = True, i + 2
                continue
            if nxt == ("word", "cubed"):
                py.append(f"({num(v)}**3)")
                say.append(f"{sayn(v)} cubed")
                prev_value, i = True, i + 2
                continue
            if nxt == ("word", "factorial"):
                if v != int(v) or not 0 <= v <= 20:
                    return None
                py.append(num(math.factorial(int(v))))
                say.append(f"{sayn(v)} factorial")
                prev_value, i = True, i + 2
                continue
            py.append(num(v))
            say.append(sayn(v))
            prev_value = True
        elif kind == "op":
            if v in ("*", "/", "**") and not prev_value:
                return None
            py.append(v)
            say.append(_SAY.get(v, v))
            prev_value = False
        elif kind == "paren":
            if v == "(" and prev_value:
                return None
            py.append(v)
            say.append(v)
            prev_value = v == ")"
        elif kind == "word":
            if v in ("sqrt", "cbrt") and not prev_value:
                skip = 2 if nxt == ("word", "of") else 1  # "square root of 144" / "square root 144"
                arg = toks[i + skip] if i + skip < len(toks) else None
                if not arg or arg[0] != "num":
                    return None
                x = arg[1]
                if x < 0:
                    return None
                py.append(num(round(math.sqrt(x), 10) if v == "sqrt" else round(x ** (1 / 3), 10)))
                say.append(f"the {'square' if v == 'sqrt' else 'cube'} root of {sayn(x)}")
                prev_value, i = True, i + skip + 1
                continue
            if v == "half" and nxt == ("word", "of") and i + 2 < len(toks) and toks[i + 2][0] == "num":
                py.append(f"({num(toks[i + 2][1])}/2)")
                say.append(f"half of {sayn(toks[i + 2][1])}")
                prev_value, i = True, i + 3
                continue
            if v in ("double", "twice", "triple") and nxt and nxt[0] == "num" and not prev_value:
                py.append(f"({num(nxt[1])}*{3 if v == 'triple' else 2})")
                say.append(f"{v} {sayn(nxt[1])}")
                prev_value, i = True, i + 2
                continue
            if v == "the":
                i += 1
                continue
            return None
        i += 1
    if not prev_value or not py:
        return None
    spoken = re.sub(r"\(\s+", "(", " ".join(say))
    return "".join(f" {p} " if p in _SAY else p for p in py).replace("  ", " "), re.sub(r"\s+\)", ")", spoken)


def fmt(x):
    """A result as it is read out: whole numbers with thousands commas, otherwise up to four decimals."""
    if abs(x - round(x)) < 1e-9:
        return f"{int(round(x)):,}"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def calc(text):
    """{"value", "say"} for a spoken sum ("what's seventeen times twenty three", "15% of 80", "square root of
    144", "9 squared"), else None. The whole utterance must be the sum: any other word and it is not one."""
    t = _TAIL.sub("", _STEM.sub("", (text or "").strip(), count=1)).strip().rstrip("?.! ")
    if not t or len(t) > 120:
        return None
    toks = _tokens(t)
    if not toks or not any(k == "num" for k, _ in toks):
        return None
    built = _build(toks)
    if not built:
        return None
    expr, say = built
    try:
        value = _eval(ast.parse(expr.strip(), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError):
        return None
    if not math.isfinite(value) or abs(value) >= 1e15:
        return None
    if len(toks) == 1:  # a bare number is not a question
        return None
    return {"value": value, "say": f"{say} is {fmt(value)}"}


# ---- units -------------------------------------------------------------------------------------------------

# name -> (dimension, size in the dimension's base unit); the first alias is the singular, the second the plural
_UNIT_TABLE = {
    "length": (1.0, {"meter": ("meter", "meters", "metre", "metres", "m"), "kilometer": ("kilometer", "kilometers", "kilometre", "kilometres", "km"),
                     "centimeter": ("centimeter", "centimeters", "centimetre", "centimetres", "cm"),
                     "millimeter": ("millimeter", "millimeters", "millimetre", "millimetres", "mm"),
                     "mile": ("mile", "miles", "mi"), "yard": ("yard", "yards", "yd"), "foot": ("foot", "feet", "ft"),
                     "inch": ("inch", "inches", "in")},
               {"meter": 1.0, "kilometer": 1000.0, "centimeter": 0.01, "millimeter": 0.001, "mile": 1609.344,
                "yard": 0.9144, "foot": 0.3048, "inch": 0.0254}),
    "mass": (1.0, {"gram": ("gram", "grams", "g"), "kilogram": ("kilogram", "kilograms", "kilo", "kilos", "kg"),
                   "milligram": ("milligram", "milligrams", "mg"), "pound": ("pound", "pounds", "lb", "lbs"),
                   "ounce": ("ounce", "ounces", "oz")},
             {"gram": 1.0, "kilogram": 1000.0, "milligram": 0.001, "pound": 453.59237, "ounce": 28.349523125}),
    "volume": (1.0, {"liter": ("liter", "liters", "litre", "litres", "l"), "milliliter": ("milliliter", "milliliters", "millilitre", "millilitres", "ml"),
                     "gallon": ("gallon", "gallons", "gal"), "quart": ("quart", "quarts", "qt"),
                     "pint": ("pint", "pints", "pt"), "cup": ("cup", "cups"),
                     "fluid ounce": ("fluid ounce", "fluid ounces", "fl oz"),
                     "tablespoon": ("tablespoon", "tablespoons", "tbsp"), "teaspoon": ("teaspoon", "teaspoons", "tsp")},
               {"liter": 1.0, "milliliter": 0.001, "gallon": 3.785411784, "quart": 0.946352946, "pint": 0.473176473,
                "cup": 0.2365882365, "fluid ounce": 0.0295735295625, "tablespoon": 0.01478676478125,
                "teaspoon": 0.00492892159375}),
    "time": (1.0, {"second": ("second", "seconds", "sec", "secs"), "minute": ("minute", "minutes", "min", "mins"),
                   "hour": ("hour", "hours", "hr", "hrs"), "day": ("day", "days"), "week": ("week", "weeks"),
                   "millisecond": ("millisecond", "milliseconds", "ms")},
             {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0, "week": 604800.0, "millisecond": 0.001}),
    "temperature": (1.0, {"celsius": ("celsius", "centigrade", "c"), "fahrenheit": ("fahrenheit", "f"),
                          "kelvin": ("kelvin", "k")}, {"celsius": 1.0, "fahrenheit": 1.0, "kelvin": 1.0}),
}
_ALIAS = {}   # alias -> (dimension, canonical)
_PLURAL = {}  # canonical -> (singular, plural) as said
for _dim, (_, _names, _f) in _UNIT_TABLE.items():
    for _canon, _aliases in _names.items():
        _PLURAL[_canon] = (_aliases[0], _aliases[1] if len(_aliases) > 1 else _aliases[0])
        for _a in _aliases:
            _ALIAS.setdefault(_a, (_dim, _canon))
_PLURAL["celsius"] = _PLURAL["fahrenheit"] = ("degree Celsius", "degrees Celsius")
_PLURAL["fahrenheit"] = ("degree Fahrenheit", "degrees Fahrenheit")
_PLURAL["kelvin"] = ("kelvin", "kelvins")
_SHORT_AMBIGUOUS = {"m", "g", "l", "c", "f", "k", "in", "mi", "pt", "gal", "min", "ms"}  # only after a number, or by name

_UNIT_RE = "|".join(sorted((re.escape(a) for a in _ALIAS if a not in _SHORT_AMBIGUOUS), key=len, reverse=True))
_UNIT = rf"(?:degrees?\s+)?(?:{_UNIT_RE})"
_NUM = r"(?:-?\d[\d,]*(?:\.\d+)?|an?|one|half|(?:[a-z]+(?:[ -][a-z]+){0,3}))"


def _unit(name):
    name = re.sub(r"^degrees?\s+", "", name.strip().lower())
    return _ALIAS.get(name)


def _number(text):
    t = text.strip().lower().replace(",", "")
    if t in ("a", "an", "one"):
        return 1.0
    if t == "half":
        return 0.5
    if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return float(t)
    toks = _tokens(t)
    if toks and len(toks) == 1 and toks[0][0] == "num":
        return toks[0][1]
    return None


_PATTERNS = [
    # how many feet in a mile / how many feet are in 3 miles / how many feet is 3 miles
    re.compile(rf"^how many (?P<to>{_UNIT}) (?:are |is )?(?:there )?(?:in|per) (?:(?P<n>{_NUM}) )?(?P<from>{_UNIT})$"),
    re.compile(rf"^how many (?P<to>{_UNIT}) (?:are|is|make up|in) (?P<n>{_NUM}) (?P<from>{_UNIT})$"),
    re.compile(rf"^how (?:much|many) is (?P<n>{_NUM}) (?P<from>{_UNIT}) in (?P<to>{_UNIT})$"),
    re.compile(rf"^(?:convert|change|turn) (?P<n>{_NUM}) (?P<from>{_UNIT}) (?:in)?to (?P<to>{_UNIT})$"),
    re.compile(rf"^(?:what is|what's|whats) (?P<n>{_NUM}) (?P<from>{_UNIT}) in (?P<to>{_UNIT})$"),
    re.compile(rf"^(?:what is|what's|whats) (?P<n>{_NUM}) (?P<from>{_UNIT}) (?:in)?to (?P<to>{_UNIT})$"),
    re.compile(rf"^(?:what is|what's|whats) (?P<n>{_NUM}) (?P<from>{_UNIT}) as (?:a |an )?(?P<to>{_UNIT})$"),
    re.compile(rf"^(?P<n>{_NUM}) (?P<from>{_UNIT}) (?:in)?to (?P<to>{_UNIT})$"),
    re.compile(rf"^(?P<n>{_NUM}) (?P<from>{_UNIT}) in (?P<to>{_UNIT})$"),
]
_CLEAN = re.compile(r"\b(?:please|sir|waggle|hey|um|uh|tell me|can you tell me|could you tell me)\b|[?!.,]")


def _conv(value, dim, frm, to):
    if dim == "temperature":
        c = {"celsius": value, "fahrenheit": (value - 32) * 5 / 9, "kelvin": value - 273.15}[frm]
        return {"celsius": c, "fahrenheit": c * 9 / 5 + 32, "kelvin": c + 273.15}[to]
    f = _UNIT_TABLE[dim][2]
    return value * f[frm] / f[to]


def convert(text):
    """{"value", "say", "dim"} for a spoken unit conversion between two units of the same kind (length, mass,
    volume, time, temperature), else None. "how many ounces in a pound" is mass; "fluid ounces" is volume."""
    t = " ".join(_CLEAN.sub(" ", (text or "").lower().replace("’", "'")).split())
    t = re.sub(r"\bwhat's\b", "what's", t)
    t = re.sub(r"\bhow many degrees (fahrenheit|celsius|kelvin) (?:is|are) ", r"how much is ", t)
    for pat in _PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        g = m.groupdict()
        a, b = _unit(g["from"]), _unit(g["to"])
        n = _number(g["n"]) if g.get("n") else 1.0
        if not a or not b or n is None or a[0] != b[0] or a[1] == b[1]:
            continue
        dim = a[0]
        if dim == "temperature" and "degree" not in t and not re.search(r"\d", t):
            continue
        value = _conv(n, dim, a[1], b[1])
        if not math.isfinite(value) or abs(value) >= 1e15:
            return None
        one, many = _PLURAL[a[1]], _PLURAL[b[1]]
        src = one[0] if n == 1 else one[1]
        dst = many[0] if abs(value - 1) < 1e-9 else many[1]
        return {"value": value, "dim": dim, "say": f"{fmt(n)} {src} is {fmt(value)} {dst}"}
    return None


def mismatched_units(text):
    """True when the text names two units of different kinds ("how many cups in a pound"): never answered by guessing."""
    names = [m.group(0) for m in re.finditer(rf"\b(?:{_UNIT_RE})\b", (text or "").lower())]
    dims = {_ALIAS[n][0] for n in names if n in _ALIAS}
    return len(dims) > 1


# ---- numbers in an answer ----------------------------------------------------------------------------------

def numbers_in(text):
    """The numbers in a sentence, digits or words, as floats ("about 8,849 metres", "fifty states")."""
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", (text or "").lower())
    toks = re.findall(r"\d+(?:\.\d+)?|[a-z]+", t)
    out, i = [], 0
    while i < len(toks):
        if toks[i][0].isdigit():
            out.append(float(toks[i]))
            i += 1
            continue
        got = _spoken_number(toks, i) if toks[i] not in ("a", "and", "oh") else None
        if got:
            out.append(float(got[0]))
            i = got[1]
        else:
            i += 1
    return out


def same_numbers(a, b, tol=0.01):
    """True when every number in `a` has a match (within tol, relative) among those in `b`, and the other way round."""
    na, nb = numbers_in(a), numbers_in(b)

    def covered(xs, ys):
        return all(any(abs(x - y) <= tol * max(abs(x), abs(y), 1e-9) for y in ys) for x in xs)
    return covered(na, nb) and covered(nb, na)
