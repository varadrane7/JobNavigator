"""One way to read a company or school name, shared by audience matching and the E-Verify table.

`words` folds accents and case and takes off legal suffixes at the end only ("Stripe, Inc." -> stripe),
so "Stripe, Inc." and "Stripe" are one name, while "Tata Technologies" and "Tata Consultancy Services" stay
different ones (curator/scoring.co_words drops words like "technologies" and would merge them).
"""
import re

LEGAL = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation", "co", "company",
         "pvt", "plc", "gmbh", "ag", "sa", "bv", "pbc", "the"}


def words(name) -> list:
    from backend.analyzer.location import fold
    ws = re.findall(r"[a-z0-9]+", fold(str(name or "")).replace("&", " and "))
    while ws and ws[-1] in LEGAL and len(ws) > 1:
        ws.pop()
    if ws and ws[0] == "the" and len(ws) > 1:
        ws.pop(0)
    return ws


def company_key(name) -> str:
    return " ".join(words(name))
