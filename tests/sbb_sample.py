"""A small SBB file with the structure of the real ``dossiers.xml`` (fictitious content).

It covers what the parser has to handle: basic and profile core tasks, two profiles that
share one crebo, a changed version (same crebos), a revision with a new crebo, two
versions with the same predecessor, and a concept dossier that is skipped.
"""

from __future__ import annotations


def _profile(nr: str, previous: str, title: str, level: str, crebos: str, tasks: str = "") -> str:
    return f"""
      <profiel nr="{nr}" nrvoorganger="{previous}" titel="{title}" hoofdstuk="1" niveau="{level}">
        <omschrijving><typeringvanhetberoep>x</typeringvanhetberoep></omschrijving>
        <creboinformatie>{crebos}</creboinformatie>
        <profielkerntaken>{tasks}</profielkerntaken>
      </profiel>"""


def _crebo(crebo: str, start: str, end: str = "") -> str:
    return (
        f'<crebo ministerie="OCW" prijsfactor="1,10" Geldig_vanaf="{start}" '
        f'Datum_einde_instroom="" Datum_einde_opleiding="{end}">{crebo}</crebo>'
    )


def _task(code: str, title: str, processes: list[tuple[str, str]]) -> str:
    wps = "".join(
        f'<werkproces nr="1" nrvoorganger="" titel="{t}" hoofdstuk="{c}">'
        "<werkprocesomschrijving/></werkproces>"
        for c, t in processes
    )
    return (
        f'<kerntaak nr="1" nrvoorganger="" titel="{title}" hoofdstuk="{code}">'
        f"<complexiteit/><basiswerkprocessen>{wps}</basiswerkprocessen></kerntaak>"
    )


def _profile_task(code: str, title: str, processes: list[tuple[str, str]]) -> str:
    return _task(code, title, processes).replace("basiswerkprocessen", "profielwerkprocessen")


def _dossier(
    nr: str, previous: str, version: str, title: str, dossier_crebo: str, start: str,
    profiles: str, *, status: str = "Vastgesteld", w2_title: str = "Installeert systemen",
) -> str:  # fmt: skip
    basic = _task(
        "B1-K1", "Richt systemen in",
        [("B1-K1-W1", "Inventariseert wensen"), ("B1-K1-W2", w2_title)],
    ) + _task("B1-K2", "Beheert systemen", [("B1-K2-W1", "Lost storingen op")])  # fmt: skip
    return f"""
  <dossier nr="{nr}" nrvoorganger="{previous}" titel="{title}" versie="{version}" status="{status}">
    <dossiercrebos><dossiercrebo ministerie="OCW" Geldig_vanaf="{start}">{dossier_crebo}</dossiercrebo></dossiercrebos>
    <kennisenvaardigheden><kennisofvaardigheid nr="1" titel="x"/></kennisenvaardigheden>
    <basis titel="Basisdeel"><basistaken titel="t"><basiskerntaken>{basic}</basiskerntaken></basistaken></basis>
    <profielen titel="Profielen">{profiles}</profielen>
  </dossier>"""


P1 = _profile_task("P1-K1", "Ontwerpt infrastructuur", [("P1-K1-W1", "Maakt een ontwerp")])
P2 = _profile_task("P2-K1", "Beveiligt systemen", [("P2-K1-W1", "Voert een audit uit")])


def sample_xml() -> bytes:
    dossiers = "".join([
        # First version: two outflows, the second has two variant profiles on one crebo.
        _dossier("100", "", "", "ICT systemen", "29000", "01-08-2015",
            _profile("1001", "", "Medewerker ICT", "3", _crebo("29001", "01-08-2015", "01-08-2025"))
            + _profile("1002", "", "Variant A", "4", _crebo("29002", "01-08-2015", "01-08-2025"), P1)
            + _profile("1003", "", "Variant B", "4", _crebo("29002", "01-08-2015", "01-08-2025"), P2)),
        # Changed in 2016, same crebos.
        _dossier("200", "100", "Gewijzigd 2016", "ICT systemen", "29000", "01-08-2015",
            _profile("2001", "1001", "Medewerker ICT", "3", _crebo("29001", "01-08-2015", "01-08-2025"))
            + _profile("2002", "1002", "Variant A", "4", _crebo("29002", "01-08-2015", "01-08-2025"), P1),
            w2_title="Installeert en configureert systemen"),
        # A second change on the same predecessor (SBB does this: "Gewijzigd 2016-2").
        _dossier("250", "100", "Gewijzigd 2016-2", "ICT systemen", "29000", "01-08-2015",
            _profile("2501", "1001", "Medewerker ICT", "3", _crebo("29001", "01-08-2015", "01-08-2025"))),
        # Revision 2024: a new dossier crebo and a new crebo for the first outflow.
        _dossier("300", "200", "Gewijzigd 2024", "ICT systemen", "29100", "01-08-2024",
            _profile("3001", "2001", "Medewerker ICT", "3", _crebo("29101", "01-08-2024"))),
        _dossier("400", "", "", "Concept", "29900", "01-08-2027",
            _profile("4001", "", "Concept", "2", _crebo("29901", "01-08-2027")), status="Concept"),
    ])  # fmt: skip
    return f"""<?xml version="1.0" encoding="utf-8"?>
<sbb>
  <domeinen><domein nr="1" titel="ICT" crebo="79050"/></domeinen>
  <competenties><competentie nr="1" code="A" titel="x"/></competenties>
  <dossiers>{dossiers}
  </dossiers>
</sbb>""".encode()
