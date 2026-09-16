#!/usr/bin/env python3
"""Print L5X rung text with every identifier replaced by a placeholder.

Preserves instruction mnemonics, operand counts, operand positions, branch
structure, and numeric literals. Replaces every tag, member, routine, and
program name with a stable placeholder. The name-to-placeholder mapping is
built per file and never written anywhere, so the output shows the shape of
the logic without disclosing what the logic is about.

    XIC(Conveyor_3_Running)XIO(EStop_OK)OTE(Mtr_3_Run);
becomes
    XIC(t1)XIO(t2)OTE(t3);

Mnemonics are preserved as-is, on the assumption that the instruction set is
vendor vocabulary rather than customer information. If a house Add-On
Instruction is named after the process, use --scrub-unknown-mnemonics to
replace any mnemonic outside the built-in list with AOI_n instead.

Usage:
    python l5x_scrub_rungs.py ./corpus --mnemonic MOVE
    python l5x_scrub_rungs.py ./corpus --mnemonic MOVE --limit 40
    python l5x_scrub_rungs.py ./corpus            # a sample of all rungs
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from l5x_text import scan_instructions

LITERAL_START = set("0123456789-+.$'\"")

# Built-in Logix instructions, so that anything else can be flagged as a
# probable Add-On Instruction. Not exhaustive; extend as needed.
BUILTIN_MNEMONICS = {
    "XIC", "XIO", "OTE", "OTL", "OTU", "ONS", "OSR", "OSF", "AFI", "NOP",
    "TND", "JMP", "LBL", "JSR", "SBR", "RET", "JXR", "MCR", "UID", "UIE",
    "TON", "TOF", "RTO", "CTU", "CTD", "RES",
    "MOV", "MVM", "COP", "CPS", "FLL", "CLR", "BTD", "SWPB",
    "ADD", "SUB", "MUL", "DIV", "MOD", "SQR", "NEG", "ABS", "CPT",
    "EQU", "NEQ", "LES", "GRT", "LEQ", "GEQ", "CMP", "LIM", "MEQ",
    "AND", "OR", "XOR", "NOT", "BAND", "BOR", "BXOR", "BNOT",
    "GSV", "SSV", "MSG", "PID", "SCL", "SCP",
    "FAL", "FSC", "COP", "AVE", "SRT", "STD",
    "DTOS", "RTOS", "STOD", "STOR", "CONCAT", "INSERT", "DELETE", "MID",
    "FIND", "LEN", "UPPER", "LOWER", "TRN",
    "FFL", "FFU", "LFL", "LFU", "BSL", "BSR",
    "EVENT", "SFR", "SFP",
}


def load_root(path: Path):
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"{path.name}: parse error: {exc}", file=sys.stderr)
        return None


def text_of(elem) -> str:
    return "".join(elem.itertext())


class Scrubber:
    """Maps identifiers to stable placeholders within one file."""

    def __init__(self, scrub_unknown_mnemonics: bool):
        self.tags: dict[str, str] = {}
        self.mnemonics: dict[str, str] = {}
        self.scrub_unknown = scrub_unknown_mnemonics

    def tag(self, name: str) -> str:
        if name not in self.tags:
            self.tags[name] = f"t{len(self.tags) + 1}"
        return self.tags[name]

    def mnemonic(self, name: str) -> str:
        if not self.scrub_unknown or name in BUILTIN_MNEMONICS:
            return name
        if name not in self.mnemonics:
            self.mnemonics[name] = f"AOI_{len(self.mnemonics) + 1}"
        return self.mnemonics[name]

    def operand(self, operand: str) -> str:
        """Rewrite one operand, keeping literals and structural punctuation."""
        s = operand.strip()
        if not s:
            return s
        # Named AOI parameter binding, e.g. "Speed := Motor_Speed"
        if ":=" in s:
            left, right = s.split(":=", 1)
            return f"{self.operand(left)} := {self.operand(right)}"
        if s[0] in LITERAL_START:
            return s
        if s.startswith("?"):
            return "?"
        # Preserve array-index and member structure, scrub the names.
        indices = re.findall(r"\[([^\]]*)\]", s)
        skeleton = re.sub(r"\[[^\]]*\]", "[]", s)
        parts = skeleton.split(".")
        base = parts[0]
        if ":" in base:
            # Module I/O reference such as Local:1:I. Keep the shape only.
            rewritten = "IO:n:X"
        else:
            rewritten = self.tag(base.replace("[]", ""))
            if "[]" in base:
                rewritten += "[]"
        out = rewritten
        for member in parts[1:]:
            clean = member.replace("[]", "")
            if clean.isdigit():
                out += f".{clean}"          # bit index, keep
            elif not clean:
                out += "."
            else:
                out += f".{self.tag(clean)}"
            if "[]" in member:
                out += "[]"
        # Put index expressions back in scrubbed form.
        for idx in indices:
            out = out.replace("[]", f"[{self.operand(idx)}]", 1)
        return out

    def rung(self, text: str) -> str:
        """Rewrite a whole rung, preserving everything between instructions."""
        result: list[str] = []
        cursor = 0
        for mnemonic, args, start, end, _closed in scan_instructions(text):
            # Nested calls are consumed as operands of the enclosing
            # instruction, so skip any match that falls inside one already
            # rewritten.
            if start < cursor:
                continue
            result.append(text[cursor:start])
            scrubbed = ",".join(self.operand(a) for a in args if a.strip())
            result.append(f"{self.mnemonic(mnemonic)}({scrubbed})")
            cursor = end
        result.append(text[cursor:])
        return "".join(result)


def find_files(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            found.extend(f for f in sorted(path.rglob("*"))
                         if f.is_file() and f.suffix.lower() == ".l5x")
        elif path.is_file():
            found.append(path)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--mnemonic", help="only rungs containing this mnemonic")
    ap.add_argument("--limit", type=int, default=25,
                    help="max rungs printed per file (default 25)")
    ap.add_argument("--scrub-unknown-mnemonics", action="store_true",
                    help="replace non-builtin mnemonics with AOI_n")
    args = ap.parse_args()

    files = find_files(args.paths)
    if not files:
        print("no .L5X files found", file=sys.stderr)
        return 1

    for path in files:
        root = load_root(path)
        if root is None:
            continue
        scrub = Scrubber(args.scrub_unknown_mnemonics)
        matches: list[str] = []
        seen = 0
        for routine in root.iter("Routine"):
            rtype = routine.attrib.get("Type", "?")
            for rung in routine.iter("Rung"):
                t = rung.find("Text")
                body = text_of(t) if t is not None else ""
                if args.mnemonic and not re.search(
                        r"\b" + re.escape(args.mnemonic) + r"\(", body):
                    continue
                seen += 1
                if len(matches) < args.limit:
                    matches.append(f"  [{rtype}] {scrub.rung(body)}")

        # File name is not printed; the corpus order is enough to identify it.
        print(f"\n=== file {files.index(path)} - {seen} matching rung(s), "
              f"showing {len(matches)} ===")
        for line in matches:
            print(line)
        if scrub.mnemonics:
            print(f"  ({len(scrub.mnemonics)} non-builtin mnemonics scrubbed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())