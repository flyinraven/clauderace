"""A station that records its condition everywhere except the diagnosis field.

Block 2A of 2020 Semester 2 has no DIAGNOSIS heading at all - the report names
it in the SUMMARY OF CASE instead, "An infant with pseudoesotropia". Eleven
stations are like that, and the empty field is not harmless:

`leaked_term` matches a description against the diagnosis, so an empty one
passes everything. The guard is not lenient there, it is absent.
"""

from __future__ import annotations

from app.services.osce.diagnosis import name_the_condition, stations_missing_a_diagnosis
from tests.test_api_osce import make_station


class _Client:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, **kwargs):
        return self.payload


def test_the_condition_named_in_the_summary_is_taken(db):
    station = make_station(db)
    station.diagnosis = None
    station.case_summary = "An infant with pseudoesotropia."

    assert name_the_condition(_Client({"diagnosis": "Pseudoesotropia"}), station) == "Pseudoesotropia"


def test_a_summary_naming_no_condition_keeps_its_empty_field(db):
    """A guess is worse than nothing: the leak guard would start withholding
    lines for a diagnosis nobody ever recorded."""
    station = make_station(db)
    station.diagnosis = None
    station.case_summary = "A painful red eye for one day."

    assert name_the_condition(_Client({"diagnosis": ""}), station) is None


def test_a_condition_not_in_the_summary_is_refused(db):
    """The instruction says quote. This is the check.

    Left unchecked the model works the diagnosis out from the presentation,
    which is the candidate's job and would put the answer in the one field the
    leak guard reads.
    """
    station = make_station(db)
    station.diagnosis = None
    station.case_summary = "A 36-year-old woman with a painful left eye after sleeping in contact lenses."

    assert name_the_condition(_Client({"diagnosis": "Pseudomonas keratitis"}), station) is None


def test_only_stations_with_something_to_read_are_selected(db):
    station = make_station(db)
    station.diagnosis = None
    station.case_summary = "An infant with pseudoesotropia."
    already = make_station(db)
    already.diagnosis = "Keratoconus"
    already.case_summary = "A 28-year-old with keratoconus."
    blank = make_station(db)
    blank.diagnosis = None
    blank.case_summary = None
    db.commit()

    selected = stations_missing_a_diagnosis(db)

    assert station.id in selected
    assert already.id not in selected
    assert blank.id not in selected, "nothing to read it from"
