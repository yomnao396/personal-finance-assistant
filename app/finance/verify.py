# app/finance/verify.py
# Checks that numbers shown to the user add up.
#
# * allocate_cuts() splits a savings target over spending lines so that the cuts
#   add up to exactly the target (whole units), never more than a line's spending.
# * validate_allocation() confirms a list of parts sums to the total it claims.
# * check_arithmetic() finds "a + b + c = d" and listed parts with a stated total
#   in a written answer (the model's) and corrects any result that is wrong, so
#   the model can explain numbers but never invent a sum.

import re
from decimal import ROUND_DOWN, Decimal

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
OPS = {"+": "+", "-": "-", "−": "-", "–": "-", "×": "*", "x": "*", "*": "*", "÷": "/", "/": "/"}
EXPRESSION = re.compile(rf"(?<![\d.,])((?:{NUMBER})(?:\s*[+\-−–×x*÷/]\s*(?:{NUMBER}))+)\s*=\s*(-?(?:{NUMBER}))"
                        r"(?![\d])")
TOTAL_LINE = re.compile(r"\b(?:total|together|in all|altogether|sum)\b|المجموع|الإجمالي|الاجمالي|إجمالي|اجمالي|معًا|معا",
                        re.IGNORECASE)
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _d(text):
    return Decimal(text.replace(",", ""))


def _shown(value, like):
    """value written the way the original result was (decimals and thousands separators)."""
    places = len(like.split(".")[1]) if "." in like else 0
    q = Decimal(1).scaleb(-places)
    return f"{value.quantize(q):,}" if "," in like else f"{value.quantize(q)}"


def validate_allocation(parts, total, tolerance=Decimal("0.01")):
    """True when the parts add up to the total."""
    return abs(sum((Decimal(str(p)) for p in parts), Decimal(0)) - Decimal(str(total))) <= tolerance


def allocate_cuts(target, lines):
    """Split `target` over [(label, monthly amount)] in proportion to each amount.

    Whole units; the rounding remainder goes to the largest line; no cut exceeds
    its line. Returns [(label, amount, cut)] adding up to exactly `target`, or
    None when the lines together are smaller than the target."""
    target = Decimal(str(target))
    lines = [(label, Decimal(str(a))) for label, a in lines if Decimal(str(a)) > 0]
    pool = sum((a for _, a in lines), Decimal(0))
    if target <= 0 or pool < target:
        return None
    lines.sort(key=lambda x: (-x[1], x[0]))
    cuts = [min(a, (target * a / pool).quantize(Decimal(1), rounding=ROUND_DOWN)) for _, a in lines]
    left = target - sum(cuts, Decimal(0))
    i = 0
    while left > 0:                     # the remainder, a unit at a time, largest lines first
        room = lines[i % len(lines)][1] - cuts[i % len(lines)]
        step = min(room, left, Decimal(1) if left >= 1 else left)
        cuts[i % len(lines)] += step
        left -= step
        i += 1
    out = [(label, a, c) for (label, a), c in zip(lines, cuts) if c > 0]
    assert validate_allocation([c for _, _, c in out], target)
    return out


def _evaluate(expression):
    tokens = re.findall(rf"{NUMBER}|[+\-−–×x*÷/]", expression)
    values, ops = [_d(tokens[0])], []
    for op, n in zip(tokens[1::2], tokens[2::2]):
        ops.append(OPS[op])
        values.append(_d(n))
    # × and ÷ first, then + and − from the left
    stack, signs = [values[0]], []
    for op, v in zip(ops, values[1:]):
        if op == "*":
            stack[-1] *= v
        elif op == "/":
            if v == 0:
                return None
            stack[-1] /= v
        else:
            signs.append(op)
            stack.append(v)
    result = stack[0]
    for op, v in zip(signs, stack[1:]):
        result = result + v if op == "+" else result - v
    return result


def _close(a, b):
    return abs(a - b) <= max(Decimal("0.01"), abs(b) * Decimal("0.005"))


def check_arithmetic(text):
    """(corrected text, [problems]). A wrong "a + b = c" has c replaced by the right
    value; listed parts whose stated total is wrong get a correction line."""
    if not text:
        return text, []
    plain = text.translate(ARABIC_DIGITS)            # same length: positions still match
    problems, edits = [], []
    for m in EXPRESSION.finditer(plain):
        if not re.search(r"[+\-−–×*÷/]|\sx\s", m.group(1)):
            continue
        right = _evaluate(m.group(1))
        stated = _d(m.group(2).lstrip("-")) * (-1 if m.group(2).startswith("-") else 1)
        if right is None or _close(stated, right):
            continue
        problems.append(f"{m.group(1)} = {m.group(2)} (should be {_shown(right, m.group(2))})")
        edits.append((m.start(2), m.end(2), _shown(right, m.group(2).lstrip("-"))))
    for start, end, new in reversed(edits):
        text = text[:start] + new + text[end:]
    # "- Lunch: 100\n- Taxi: 60\nTotal: 500": each bullet with one amount, then a total line.
    lines, run, notes = text.split("\n"), [], []
    for line in lines:
        flat = line.translate(ARABIC_DIGITS)
        found = re.findall(NUMBER, flat)
        if BULLET.match(flat) and len(found) == 1 and not TOTAL_LINE.search(flat):
            run.append(_d(found[0]))
            continue
        if run and len(run) >= 2 and TOTAL_LINE.search(flat) and found:
            stated, right = _d(found[-1]), sum(run, Decimal(0))
            if not any(_close(_d(f), right) for f in found):
                problems.append(f"listed amounts add up to {right}, not {stated}")
                notes.append(" + ".join(f"{v:,}" for v in run) + f" = {right:,}")
        run = []
    if notes:
        arabic = bool(re.search(r"[ء-ي]", text))
        text += "\n\n" + ("تصحيح: " if arabic else "Correction: ") + "; ".join(notes)
    return text, problems


def amounts_in(text):
    """Every amount written in a text, as Decimals (Arabic digits read too)."""
    plain = (text or "").translate(ARABIC_DIGITS)
    return [_d(n) for n in re.findall(rf"(?<![\d.,])(?:{NUMBER})(?![\d])", plain)]


def unsupported_amounts(answer, allowed, minimum=Decimal(100)):
    """Money-sized amounts in an answer that none of the allowed figures (tool
    results, the question, the user's month totals) contains. Years, small counts
    and percentages are not checked."""
    allowed = {Decimal(str(a)).quantize(Decimal("0.01")) for a in allowed if a is not None}
    plain = (answer or "").translate(ARABIC_DIGITS)
    out = []
    for m in re.finditer(rf"(?<![\d.,])({NUMBER})(?![\d])(\s*%)?", plain):
        value = _d(m.group(1))
        if m.group(2) or value < minimum or (1900 <= value <= 2100 and "," not in m.group(1)):
            continue
        if value.quantize(Decimal("0.01")) not in allowed and value not in out:
            out.append(value)
    return out


def unsupported_percents(answer, allowed, tolerance=Decimal("0.05")):
    """Percentages in an answer that none of the allowed ones matches (rounded the
    way a sentence would round them: 49.7% and 50% both match 49.70)."""
    plain = (answer or "").translate(ARABIC_DIGITS)
    allowed = [Decimal(str(a)) for a in allowed if a is not None]
    out = []
    for m in re.finditer(rf"(?<![\d.,])({NUMBER})\s*%", plain):
        value = _d(m.group(1))
        places = len(m.group(1).split(".")[1]) if "." in m.group(1) else 0
        step = Decimal(1).scaleb(-places) / 2 + tolerance
        if not any(abs(value - a) <= step for a in allowed) and value not in out:
            out.append(value)
    return out
