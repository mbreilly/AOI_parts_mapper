"""Reads a bill of materials (CSV, or XLSX when openpyxl is installed), works out which column is
which, expands the reference designators, and suggests which parts are polarised."""

import csv
import io
import re
from pathlib import Path

FIELDS = {
    "refdes": ["part reference", "reference", "references", "designator", "designators", "refdes", "ref des",
               "ref", "refs", "reference designator", "reference designators"],
    "value": ["value", "part number", "mpn", "manufacturer part number", "mfr part number", "mfg part number",
              "part no", "pn", "comment"],
    "description": ["description", "desc", "part description", "comment"],
    "type": ["part type", "type", "category", "class"],
    "dnp": ["dnp", "populate", "fitted", "do not place", "dni", "not fitted", "assembly"],
}

KINDS = {"tantalum": "Tantalum capacitor", "electrolytic": "Electrolytic capacitor", "diode": "Diode",
         "led": "LED", "ic": "IC (pin 1)", "other": "Other polarised part"}

# Default keyword rules, checked in order against the upper-cased description, value and type.
DEFAULT_RULES = [
    {"kind": None, "any": ["TVS", "ESD", "BIDIR"], "unless": ["SCHOTTKY"], "note": "bidirectional protection diode"},
    {"kind": "tantalum", "any": ["TANT"]},
    {"kind": "electrolytic", "any": ["ALUM", "ELECTROLYTIC", "POLYMER"]},
    {"kind": "led", "any": ["LED"]},
    {"kind": "diode", "any": ["SCHOTTKY", "ZENER", "RECTIFIER", "DIODE"]},
    {"kind": "ic", "starts": ["IC"], "any": ["CONVERTER", "MCU", "FPGA", "SENSOR", "MULTIPLEXER", "MUX", "REGULATOR",
                                            "OSC", "CRYSTAL", "MICROCONTROLLER", "EEPROM", "FLASH", "AMPLIFIER", "OPAMP",
                                            "OP AMP", "TRANSCEIVER", "LDO"]},
]


class BomError(Exception):
    pass


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (h or "").lower()).strip()


def read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    """The header row and data rows. The header is the first row that looks like one (some BOM
    exports start with a title block)."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as e:
            raise BomError("Reading .xlsx needs openpyxl (pip install openpyxl), or save the BOM as CSV.") from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [["" if v is None else str(v) for v in r] for r in wb.active.iter_rows(values_only=True)]
    else:
        raw = path.read_bytes()
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    known = {a for al in FIELDS.values() for a in al}
    for i, r in enumerate(rows[:40]):
        if sum(_norm(c) in known for c in r) >= 2:
            head = [c.strip() for c in r]
            return head, [r + [""] * (len(head) - len(r)) for r in rows[i + 1:] if any(c.strip() for c in r)]
    raise BomError("No header row found. The BOM needs columns such as Reference and Description.")


def guess_columns(header: list[str]) -> dict:
    """{field: column index or None}. Earlier aliases win; one column per field."""
    out, used = {}, set()
    norm = [_norm(h) for h in header]
    for f, aliases in FIELDS.items():
        out[f] = None
        for a in aliases:
            hit = next((i for i, h in enumerate(norm) if h == a and i not in used), None)
            if hit is not None:
                out[f] = hit; used.add(hit); break
    return out


def expand_refs(cell: str) -> list[str]:
    """'C1, C2 C5-C7;R3' -> [C1, C2, C5, C6, C7, R3]."""
    out = []
    for tok in re.split(r"[,\s;]+", cell or ""):
        if not tok:
            continue
        m = re.fullmatch(r"([A-Za-z_]+)(\d+)-(?:([A-Za-z_]+))?(\d+)", tok)
        if m and (not m.group(3) or m.group(3) == m.group(1)) and int(m.group(4)) >= int(m.group(2)) \
                and int(m.group(4)) - int(m.group(2)) < 1000:
            out += [f"{m.group(1)}{n}" for n in range(int(m.group(2)), int(m.group(4)) + 1)]
        else:
            out.append(tok)
    return out


def _is_dnp(v: str, header: str) -> bool:
    v, h = (v or "").strip().upper(), _norm(header)
    if h in ("populate", "fitted", "assembly"):
        return v in ("NO", "N", "FALSE", "0", "DNP", "DNF", "NOT FITTED")
    return v in ("YES", "Y", "TRUE", "1", "X", "DNP", "DNF", "DNI")


def classify(text: str, rules=None) -> tuple[str | None, str]:
    """The kind for a part from its description/value/type text, and why."""
    u = (text or "").upper().strip()
    has = lambda k: re.search(r"(?<![A-Z])" + re.escape(k.strip()), u) is not None   # at a word start
    for r in rules or DEFAULT_RULES:
        hit = next((k for k in r.get("any", []) if has(k)), None) or \
              next((k for k in r.get("starts", []) if re.match(re.escape(k) + r"(?![A-Z])", u)), None)
        if hit and not any(has(k) for k in r.get("unless", [])):
            return r["kind"], r.get("note") or f"'{hit.strip()}' in the BOM"
    return None, ""


def read_bom(path: Path, columns: dict | None = None, rules=None) -> dict:
    """Reads the BOM. Returns {"header", "columns", "lines": [...], "parts": {ref: line index},
    "dnp": [refs]}. Each line is one part number: {value, description, type, refs, kind, why}."""
    header, rows = read_table(path)
    cols = columns or guess_columns(header)
    if cols.get("refdes") is None:
        raise BomError("Couldn't tell which column holds the reference designators. Choose it in the column list.")
    get = lambda r, f: (r[cols[f]].strip() if cols.get(f) is not None and cols[f] < len(r) else "")
    lines, parts, dnp = [], {}, []
    for r in rows:
        refs = expand_refs(get(r, "refdes"))
        if not refs:
            continue
        if cols.get("dnp") is not None and _is_dnp(get(r, "dnp"), header[cols["dnp"]]):
            dnp += refs
            continue
        value, desc, typ = get(r, "value"), re.sub(r"\s+", " ", get(r, "description")), get(r, "type")
        kind, why = classify(" ".join((desc, value, typ)), rules)
        for ref in refs:
            parts[ref] = len(lines)
        lines.append({"value": value, "description": desc, "type": typ, "refs": refs, "kind": kind, "why": why})
    return {"header": header, "columns": cols, "lines": lines, "parts": parts, "dnp": dnp}
