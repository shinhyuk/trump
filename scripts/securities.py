"""Map broker-style security descriptions from 278-T filings to tickers."""
import difflib
import re
import urllib.request

LISTING_URLS = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

BOND_RE = re.compile(
    r"\d+\.\d+\s*%|%\s*D[uo]|\bDU[EO]\b|\bREV\b|\bB/?E\b|\bBIE\b|\bRFDG\b|\bBDS?\b|\bBONDS?\b"
    r"|\bNOTES?\b|\bTREAS|\bG\.?O\.?\b|\bSCH(?:OOL)? DIST|\bAUTH\b|\bCNTY\b|\bMUNI|\bOBLIG|\bCTF\b"
    r"|\bT-BILL|\bCOUPON\b|\bMATURES?\b|%|\b[CO]UE\b|\bDU[I1]\b|\bREGS\b|\bSENIOR\b|\bPERP\b"
    r"|\bTIER\b|\bNTS\b|\bDTD\b|\b\d{1,2}[/\\]\d{1,2}[/\\]\d{2,4}\b|BOND\b|\bTREAS\b|\bBILL\b",
    re.I)
CASH_RE = re.compile(r"MONEY\s*(FUND|MARKET|MKT|MAR)|OBLIGATIONS FUND|\bSWEEP\b|\bCASH\b|TREASURY (FUND|PORTFOLIO)", re.I)

ABBR = {
    "FINL": "FINANCIAL", "HLDGS": "HOLDINGS", "HLDG": "HOLDINGS", "SYS": "SYSTEMS",
    "INTL": "INTERNATIONAL", "COS": "COMPANIES", "PMTS": "PAYMENTS", "NATL": "NATIONAL",
    "ENTMT": "ENTERTAINMENT", "PAC": "PACIFIC", "LABS": "LABORATORIES", "MTRS": "MOTORS",
    "GRP": "GROUP", "SVCS": "SERVICES", "SVC": "SERVICES", "CMNTYS": "COMMUNITIES",
    "INSTRS": "INSTRUMENTS", "INSTR": "INSTRUMENTS", "MGMT": "MANAGEMENT", "PPTYS": "PROPERTIES",
    "RLTY": "REALTY", "TECHS": "TECHNOLOGIES", "TECH": "TECHNOLOGIES", "TECHNOLOGY": "TECHNOLOGIES",
    "COMM": "COMMUNICATIONS", "COMMUNICATION": "COMMUNICATIONS", "PHARMA": "PHARMACEUTICALS",
    "PHARMACEUTICAL": "PHARMACEUTICALS", "INDS": "INDUSTRIES", "MFG": "MANUFACTURING",
    "ENTERPRISES": "ENTERPRISE", "SOLUTIONS": "SOLUTION", "BANCORPORATION": "BANCORP",
    "AMERN": "AMERICAN", "AMER": "AMERICA", "ELEC": "ELECTRIC", "SCIENTIFIC": "SCIENTIFIC",
    "INFORMATIO": "INFORMATION", "DEVIC": "DEVICES", "WHSL": "WHOLESALE", "MED": "MEDICAL",
    "PETE": "PETROLEUM", "RES": "RESOURCES", "TR": "TRUST", "ENGY": "ENERGY", "SOFTWRE": "SOFTWARE",
}
STOP = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "PLC", "LTD", "LIMITED", "THE", "NEW", "COM",
        "CL", "CLASS", "SHS", "ORD", "UNSOLICITED", "SOLICITED", "REIT", "F", "IRELAND", "COMMON",
        "STOCK", "SHARES", "SA", "NV", "AG", "SE", "LLC", "LP", "INCORPORATED", "ADR", "SPONSORED",
        "DE", "DEL", "OH", "NY", "N", "V", "A", "B", "C", "EUR", "NVF", "PAR", "VALUE", "USD", "ORDINARY", "AND", "FUND", "INDEX"}

# Brokers abbreviate Select Sector SPDR names beyond recognition; map by sector word.
SECTOR_SPDR = [("INDST", "XLI"), ("INDUSTRIAL", "XLI"), ("ENERGY", "XLE"), ("TECH", "XLK"),
               ("FINANC", "XLF"), ("FINL", "XLF"), ("HEALTH", "XLV"), ("HLTH", "XLV"), ("UTIL", "XLU"),
               ("STAPLE", "XLP"), ("STPL", "XLP"), ("DISCR", "XLY"), ("DSCR", "XLY"),
               ("MATERIAL", "XLB"), ("MATL", "XLB"), ("REAL EST", "XLRE"), ("COMMUN", "XLC")]
OVERRIDES = [
    (r"VANGUARD S.?P 500", "VOO"), (r"500 EQ\w* W", "RSP"), (r"S.?P 500 MOMENTUM", "SPMO"),
    (r"RUSSELL 1000 ETF|RUSS\w* 1000 E", "IWB"), (r"RUSSELL 2000", "IWM"), (r"MSCI EMERGING|CORE MSCI EMER", "IEMG"),
    (r"MSCI CANADA", "EWC"), (r"MSCI JAPAN", "EWJ"), (r"GOLD TRUST|GOLD TR\b", "IAU"),
    (r"MID\w* \w* ?VALUE|MSCI USA MID", "IWS"), (r"INVESCO QQQ|\bQQQ\b|NASDAQ 100", "QQQ"),
    (r"SPDR S.?P 500 ETF|SPDR S&P 500 TR", "SPY"), (r"TOTAL STOCK MKT|TOTAL STOCK MARKET", "VTI"),
    (r"ALPHABET INC CL(ASS)? ?C", "GOOG"), (r"ALPHABET", "GOOGL"), (r"BERKSHIRE.*B", "BRK-B"),
    (r"DISNEY", "DIS"), (r"META PLATFORMS", "META"), (r"MICROSOFT", "MSFT"), (r"NVIDIA", "NVDA"),
    (r"AMAZON", "AMZN"), (r"APPLE INC", "AAPL"), (r"BROADCOM", "AVGO"), (r"TESLA", "TSLA"),
    (r"ADVANCED MICRO", "AMD"), (r"PALANTIR", "PLTR"), (r"STRATEGY INC", "MSTR"),
    (r"GALLAGHER ARTHUR", "AJG"), (r"KKR", "KKR"), (r"\bIBM\b|INTL BUSINESS MACH", "IBM"),
    (r"UNITEDHEALTH|UNITEOHEALTH", "UNH"), (r"MARSH & MCLENNAN", "MRSH"), (r"\bAT&T", "T"),
    (r"PG&E", "PCG"), (r"AMA[Z2]ON", "AMZN"), (r"DATA[DO]OG", "DDOG"), (r"BOOKING H", "BKNG"),
    (r"STATE STRT CONS DSRY|CONS\w* DISCR", "XLY"), (r"CHUBB", "CB"), (r"LIN[DO]E ?PLC", "LIN"), (r"S&P GLOBAL", "SPGI"), (r"FIDELITY NATL INFO", "FIS"),
]


def kind_of(desc):
    if CASH_RE.search(desc):
        return "cash"
    if re.search(r"\bETF\b", desc, re.I):  # bond ETFs trade like stocks
        return "etf"
    if BOND_RE.search(desc):
        return "bond"
    if re.search(r"\bETF\b|\bELF\b|\bETN\b|SPDR|ISHARES|VANGUARD|INVESCO", desc, re.I):
        return "etf"
    return "stock"


def _tokens(s):
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"\b([A-Z]{3,}?)(INC|CORP)\b", r"\1 \2", s)  # "NETFLIXINC"
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    toks = [ABBR.get(t, t) for t in s.split()]
    return [t for t in toks if t not in STOP]


def norm_key(s):
    return "".join(_tokens(s))


def _listing_key(name):
    name = re.split(r" - |, Inc\.| Common Stock| Class [A-Z] |\(The\)", name)[0]
    return norm_key(name)


class Matcher:
    def __init__(self, listings):
        """listings: list of (symbol, name, is_etf)."""
        self.by_key = {}
        self.sorted_key = {}
        for sym, name, etf in listings:
            if re.search(r"\b(Warrants?|Rights?|Units?|Preferred)\b|Notes? due|%",
                         name, re.I) and not etf:
                continue
            if not re.fullmatch(r"[A-Z]{1,5}", sym):
                continue
            k = _listing_key(name)
            if len(k) < 2:
                continue
            # Prefer shorter symbols / first seen for duplicate keys (common shares).
            if k not in self.by_key or len(sym) < len(self.by_key[k][0]):
                self.by_key[k] = (sym, name)
            sk = "".join(sorted(_tokens(re.split(r" - ", name)[0])))
            self.sorted_key.setdefault(sk, (sym, name))
        self.keys = sorted(self.by_key)

    @classmethod
    def download(cls):
        listings = []
        for url in LISTING_URLS:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
            lines = text.splitlines()
            hdr = lines[0].split("|")
            for ln in lines[1:]:
                f = ln.split("|")
                if len(f) != len(hdr) or ln.startswith("File Creation"):
                    continue
                row = dict(zip(hdr, f))
                if row.get("Test Issue") == "Y":
                    continue
                sym = row.get("Symbol") or row.get("ACT Symbol")
                listings.append((sym, row["Security Name"], row.get("ETF") == "Y"))
        return cls(listings)

    def match(self, desc):
        d = desc.upper()
        if "SPDR" in d and re.search(r"S[EL]?CT|SECTOR|SCTR|SLCT", d):
            for word, sym in SECTOR_SPDR:
                if word in d:
                    return sym
        for pat, sym in OVERRIDES:
            if re.search(pat, d):
                return sym
        k = norm_key(desc)
        if len(k) < 3:
            return None
        if k in self.by_key:
            return self.by_key[k][0]
        sk = "".join(sorted(_tokens(desc)))
        if sk in self.sorted_key:
            return self.sorted_key[sk][0]
        # Truncated descriptions ("VERIZON COMMUNICATIONS I") are prefixes of the listing name.
        if len(k) >= 8:
            cands = [key for key in self.keys if key.startswith(k)]
            if cands:
                return self.by_key[min(cands, key=len)][0]
        # Or the listing name is a prefix of a description with trailing noise.
        best = None
        for n in range(len(k) - 1, 4, -1):
            if k[:n] in self.by_key:
                best = k[:n]
                break
        if best and len(best) >= 0.7 * len(k):
            return self.by_key[best][0]
        close = difflib.get_close_matches(k, self.keys, n=1, cutoff=0.86 if len(k) >= 7 else 0.95)
        if close:
            return self.by_key[close[0]][0]
        return None
