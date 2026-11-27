# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware's command table as the simulator resolves headers against it.

:data:`COMMANDS` is a copy of ``kCommands`` in ``firmware/src/console.cpp``:
every header pattern in the firmware's order, with its flags as the letters
of ``docs/scpi.md``'s Flags column (``D`` needs the INA228, ``R`` refused while
a window runs, ``C`` refused while one is open) and the most parameters its
command takes, 0 for none. A test holds the copy to the firmware's table, as
``scripts/scpi_table.py`` reads it, so a command added there fails until the
simulator implements it or names it in its undefined set.

:func:`resolve` matches a header as the firmware's ``scpi::matches`` does: a
node in its short or its long form, in any case, an optional node left out,
a numeric suffix on a ``#`` node, a query only against a query pattern.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: One row of the table: the pattern, its flags and the most parameters it takes.
Row = tuple[str, str, int]

#: ``kCommands``: (pattern, flags, most parameters), in the firmware's order.
COMMANDS: tuple[Row, ...] = (
    ("*IDN?", "", 0),
    ("*RST", "", 0),
    ("*CLS", "", 0),
    ("*ESE", "", 1),
    ("*ESE?", "", 0),
    ("*ESR?", "", 0),
    ("*SRE", "", 1),
    ("*SRE?", "", 0),
    ("*STB?", "", 0),
    ("*OPC", "", 0),
    ("*OPC?", "", 0),
    ("*WAI", "", 0),
    ("*TST?", "RC", 0),
    ("SYSTem:ERRor[:NEXT]?", "", 0),
    ("SYSTem:ERRor:COUNt?", "", 0),
    ("SYSTem:ERRor:ALL?", "", 0),
    ("SYSTem:VERSion?", "", 0),
    ("SYSTem:INFormation?", "", 0),
    ("SYSTem:STATus?", "", 0),
    ("SYSTem:HEALth?", "", 0),
    ("SYSTem:COMMunicate:I2C:FREQuency", "DR", 1),
    ("SYSTem:COMMunicate:I2C:FREQuency?", "", 0),
    ("SYSTem:LED?", "", 0),
    ("SYSTem:PINS?", "", 0),
    ("SYSTem:PINS:PENDing?", "", 0),
    ("SYSTem:PINS:STAGe", "", 3),
    ("SYSTem:PINS:ADDRess", "", 1),
    ("SYSTem:PINS:CLOCk", "", 1),
    ("SYSTem:PINS:CLEar", "", 0),
    ("SYSTem:PINS:APPLy", "RC", 0),
    ("SYSTem:PINS:DEFault", "RC", 0),
    ("SYSTem:PINS:RESult?", "", 0),
    ("STATus:OPERation[:EVENt]?", "", 0),
    ("STATus:OPERation:CONDition?", "", 0),
    ("STATus:OPERation:ENABle", "", 1),
    ("STATus:OPERation:ENABle?", "", 0),
    ("STATus:QUEStionable[:EVENt]?", "", 0),
    ("STATus:QUEStionable:CONDition?", "", 0),
    ("STATus:QUEStionable:ENABle", "", 1),
    ("STATus:QUEStionable:ENABle?", "", 0),
    ("STATus:PRESet", "", 0),
    ("STATus:QUEStionable:PTRansition", "", 1),
    ("STATus:QUEStionable:PTRansition?", "", 0),
    ("STATus:QUEStionable:NTRansition", "", 1),
    ("STATus:QUEStionable:NTRansition?", "", 0),
    ("STATus:OPERation:PTRansition", "", 1),
    ("STATus:OPERation:PTRansition?", "", 0),
    ("STATus:OPERation:NTRansition", "", 1),
    ("STATus:OPERation:NTRansition?", "", 0),
    ("SENSe:PROFile", "DRC", 1),
    ("SENSe:PROFile?", "", 0),
    ("SENSe:PROFile:REPort?", "", 0),
    ("SENSe:CURRent:RANGe[:UPPer]", "DRC", 1),
    ("SENSe:CURRent:RANGe[:UPPer]?", "", 0),
    ("SENSe:CURRent:RANGe:INDex", "DRC", 1),
    ("SENSe:CURRent:RANGe:INDex?", "", 0),
    ("SENSe:CURRent:RANGe:REPort?", "", 0),
    ("SENSe:CURRent:SHUNt", "R", 1),
    ("SENSe:CURRent:SHUNt?", "", 0),
    ("SENSe:CURRent:SHUNt:RANGe#", "R", 1),
    ("SENSe:CURRent:SHUNt:RANGe#?", "", 0),
    ("SENSe:CURRent:APERture", "DRC", 1),
    ("SENSe:CURRent:APERture?", "", 0),
    ("SENSe:VOLTage:APERture?", "", 0),
    ("SENSe:TEMPerature:APERture?", "", 0),
    ("SENSe:AVERage:COUNt?", "", 0),
    ("SENSe:AVERage:COUNt", "DRC", 1),
    ("SENSe:AVERage[:STATe]", "DRC", 1),
    ("SENSe:AVERage[:STATe]?", "", 0),
    ("SENSe:FUNCtion:ON", "R", 4),
    ("SENSe:FUNCtion:ON?", "", 0),
    ("SENSe:FUNCtion:ON:DEFault", "R", 0),
    ("SENSe:CHARge?", "DRC", 0),
    ("SENSe:CHARge:RESet", "DRC", 0),
    ("SENSe:CAPTure", "R", 1),
    ("SENSe:CAPTure?", "", 0),
    ("SENSe:CAPTure:LIMit", "R", 1),
    ("SENSe:CAPTure:LIMit?", "", 0),
    ("SENSe:CURRent:THReshold:MARK", "R", 1),
    ("SENSe:CURRent:THReshold:MARK?", "", 0),
    ("SENSe:CURRent:THReshold", "R", 1),
    ("SENSe:CURRent:THReshold?", "", 0),
    ("SENSe:CURRent:THReshold:HYSTeresis", "R", 1),
    ("SENSe:CURRent:THReshold:HYSTeresis?", "", 0),
    ("SAMPle:TIMer?", "", 0),
    ("SAMPle:COUNt", "R", 1),
    ("SAMPle:COUNt?", "", 0),
    ("FORMat[:DATA]", "R", 1),
    ("FORMat[:DATA]?", "", 0),
    ("TRIGger:SOURce", "R", 1),
    ("TRIGger:SOURce?", "", 0),
    ("TRIGger:LEVel", "R", 1),
    ("TRIGger:LEVel?", "", 0),
    ("TRIGger:SLOPe", "R", 1),
    ("TRIGger:SLOPe?", "", 0),
    ("TRIGger:PRETrigger", "R", 1),
    ("TRIGger:PRETrigger?", "", 0),
    ("CONFigure[:SCALar]:CURRent[:DC]", "DRC", 1),
    ("CONFigure[:SCALar]:VOLTage[:DC]", "DR", 0),
    ("CONFigure[:SCALar]:POWer[:DC]", "DR", 0),
    ("CONFigure[:SCALar]:ENERgy", "DR", 0),
    ("CONFigure[:SCALar]:TEMPerature", "DR", 0),
    ("CONFigure?", "", 0),
    ("MEASure[:SCALar]:CURRent[:DC]?", "DR", 1),
    ("MEASure[:SCALar]:VOLTage[:DC]?", "DR", 0),
    ("MEASure[:SCALar]:POWer[:DC]?", "DR", 0),
    ("MEASure[:SCALar]:ENERgy?", "DR", 0),
    ("MEASure[:SCALar]:TEMPerature?", "DR", 0),
    ("READ?", "DR", 0),
    ("READ[:SCALar]:CURRent[:DC]?", "DR", 0),
    ("READ[:SCALar]:VOLTage[:DC]?", "DR", 0),
    ("READ[:SCALar]:POWer[:DC]?", "DR", 0),
    ("READ[:SCALar]:ENERgy?", "DR", 0),
    ("READ[:SCALar]:TEMPerature?", "DR", 0),
    ("FETCh?", "", 0),
    ("FETCh[:SCALar]:CURRent[:DC]?", "", 0),
    ("FETCh[:SCALar]:VOLTage[:DC]?", "", 0),
    ("FETCh[:SCALar]:POWer[:DC]?", "", 0),
    ("FETCh[:SCALar]:ENERgy?", "", 0),
    ("FETCh[:SCALar]:CHARge?", "", 0),
    ("FETCh[:SCALar]:TEMPerature?", "", 0),
    ("FETCh:STATistics?", "", 0),
    ("FETCh:STATistics:BATCh?", "", 0),
    ("FETCh:HEADer?", "", 0),
    ("INITiate[:IMMediate]", "DR", 0),
    ("INITiate:FORCe", "DR", 0),
    ("ABORt", "D", 0),
    ("TRACe:MARK", "", 1),
    ("TRACe:DATA?", "R", 3),
    ("TRACe:POINts?", "", 0),
    ("TRACe:POINts:MAXimum?", "", 0),
    ("CALibration:ZERO", "DR", 1),
    ("CALibration:ZERO:RESult?", "", 0),
    ("CALibration:TAG", "R", 1),
    ("CALibration:TAG?", "", 0),
    ("CALibration:DATE", "R", 3),
    ("CALibration:DATE?", "", 0),
    ("CALibration:VBUSside", "R", 1),
    ("CALibration:VBUSside?", "", 0),
    ("CALibration:DATA?", "", 0),
    ("CALibration:SHUNt:REFerence", "R", 1),
    ("CALibration:SHUNt:REFerence?", "", 0),
    ("CALibration:SHUNt[:MEASure]", "DR", 0),
    ("CALibration:SHUNt:RESult?", "", 0),
    ("DIAGnostic:ALERt?", "DRC", 0),
    ("DIAGnostic:I2C:SCAN?", "R", 0),
    ("DIAGnostic:REGister?", "DRC", 1),
    ("DIAGnostic:HEAP?", "", 0),
    ("DIAGnostic:WATChdog?", "", 0),
    ("DIAGnostic:WATChdog:TEST", "RC", 0),
    ("ROUTe:LEAD", "", 3),
    ("ROUTe:LEAD:CYCLe", "", 3),
    ("ROUTe:BYPass", "", 3),
    ("ROUTe:NULLpath", "", 3),
    ("ROUTe:RELay#", "", 2),
    ("ROUTe:STATe?", "", 0),
    ("ROUTe:RESult?", "", 0),
)


@dataclass(frozen=True)
class Node:
    """One node of a pattern.

    :ivar name: Its long form as the pattern spells it, ``CURRent``.
    :ivar short: Its short form: the leading capitals and digits, ``CURR``.
    :ivar optional: Whether it is bracketed, ``[:DC]``.
    :ivar suffix: Whether it takes a numeric suffix, ``RANGe#``.
    """

    name: str
    short: str
    optional: bool
    suffix: bool


@dataclass(frozen=True)
class Resolved:
    """A header matched against the table.

    :ivar pattern: The firmware's pattern, ``SENSe:CURRent:SHUNt:RANGe#?``.
    :ivar key: The pattern's required short forms, ``SENS:CURR:SHUN:RANG#?``,
        which the simulator dispatches on.
    :ivar flags: The pattern's flag letters.
    :ivar most: The most parameters the command takes, 0 for none.
    :ivar suffix: The numeric suffix the header carried, or None.
    """

    pattern: str
    key: str
    flags: str
    most: int
    suffix: int | None


def compile_pattern(pattern: str) -> tuple[Node, ...]:
    """The nodes of a non-common *pattern*, its query mark dropped.

    :param pattern: A pattern of :data:`COMMANDS` not starting with ``*``.
    :returns: Its nodes, in order.
    """
    nodes = []
    for piece in pattern.rstrip("?").replace("[:", ":[").split(":"):
        optional = piece.startswith("[")
        name = piece.strip("[]")
        suffix = name.endswith("#")
        name = name.rstrip("#")
        short = ""
        for char in name:
            if not (char.isupper() or char.isdigit()):
                break
            short += char
        nodes.append(Node(name, short, optional, suffix))
    return tuple(nodes)


def key_of(pattern: str) -> str:
    """The dispatch key of *pattern*: its required nodes' short forms.

    :param pattern: A pattern of :data:`COMMANDS`.
    :returns: ``*IDN?`` for a common command, else ``SENS:CURR:SHUN:RANG#?``.
    """
    if pattern.startswith("*"):
        return pattern.upper()
    nodes = [
        node.short.upper() + ("#" if node.suffix else "")
        for node in compile_pattern(pattern)
        if not node.optional
    ]
    return ":".join(nodes) + ("?" if pattern.endswith("?") else "")


def _form_matches(node: Node, token: str) -> bool:
    """Whether *token* is *node*'s long or short form, in any case."""
    upper = token.upper()
    return upper == node.name.upper() or (bool(node.short) and upper == node.short.upper())


def _node_matches(node: Node, token: str) -> tuple[bool, int | None]:
    """Whether *token* matches *node*, and the suffix it carried."""
    if _form_matches(node, token):
        return True, None
    alpha = token.rstrip("0123456789")
    if not node.suffix or alpha == token or not alpha:
        return False, None
    if not _form_matches(node, alpha):
        return False, None
    return True, int(token[len(alpha) :])


def _match_from(
    nodes: tuple[Node, ...], s: int, tokens: list[str], t: int
) -> tuple[bool, int | None]:
    """Match *nodes* from *s* against *tokens* from *t*, as ``matchFrom`` does."""
    if s == len(nodes):
        return t == len(tokens), None
    node = nodes[s]
    if t < len(tokens):
        here, suffix = _node_matches(node, tokens[t])
        if here:
            rest, later = _match_from(nodes, s + 1, tokens, t + 1)
            if rest:
                return True, suffix if suffix is not None else later
    if node.optional:
        return _match_from(nodes, s + 1, tokens, t)
    return False, None


def matches(pattern: str, header: str) -> tuple[bool, int | None]:
    """Whether the path-resolved *header* matches *pattern*, and its suffix.

    :param pattern: A pattern of :data:`COMMANDS`.
    :param header: The header with its path resolved and no leading colon.
    :returns: The verdict and the numeric suffix the header carried, or None.
    """
    if pattern.startswith("*"):
        return pattern.upper() == header.upper(), None
    if pattern.endswith("?") != header.endswith("?"):
        return False, None
    tokens = header.rstrip("?").split(":")
    if any(not token for token in tokens):
        return False, None  # an empty node
    nodes = _COMPILED.get(pattern)
    if nodes is None:
        nodes = compile_pattern(pattern)  # a pattern of another table (a test's)
    return _match_from(nodes, 0, tokens, 0)


#: What ends a header, as the firmware's ``chars::isSpace`` says (``scpi.cpp``,
#: ``Splitter::next``): a space, a tab, a carriage return or a newline.
HEADER_END = " \t\r\n"


def split_header(unit: str) -> tuple[str, str]:
    """A unit's header and its parameters, split where the firmware ends the header.

    :param unit: One unit, leading white space already removed.
    :returns: The header, and what follows the character that ended it (empty when
        nothing did).
    """
    for index, character in enumerate(unit):
        if character in HEADER_END:
            return unit[:index], unit[index + 1 :]
    return unit, ""


def resolve(header: str, table: Sequence[Row] | None = None) -> Resolved | None:
    """The first pattern of the table *header* matches, as the firmware walks them.

    :param header: The header with its path resolved and no leading colon.
    :param table: (pattern, flags, most) rows in the firmware's order, :data:`COMMANDS`
        by default; the panel's schema test passes ``kCommands`` as read from
        ``console.cpp``.
    :returns: The match, or None for an undefined header.
    """
    for pattern, flags, most in COMMANDS if table is None else table:
        ok, suffix = matches(pattern, header)
        if ok:
            key = _KEYS.get(pattern)
            return Resolved(pattern, key_of(pattern) if key is None else key, flags, most, suffix)
    return None


_COMPILED = {pattern: compile_pattern(pattern) for pattern, *_ in COMMANDS if pattern[0] != "*"}
_KEYS = {pattern: key_of(pattern) for pattern, *_ in COMMANDS}
