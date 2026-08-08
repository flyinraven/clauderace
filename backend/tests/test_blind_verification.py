"""The gate must not be able to agree with what it was told to expect.

A nine-positions-of-gaze montage of one patient's *unilateral* Brown's syndrome
was captioned "bilateral" at confidence 1.00, and the notes stored beneath it
restated the station's own recorded findings almost word for word:

    station findings : limited elevation in adduction, often with a downshoot
                       in full elevation
    verification says: limited elevation in adduction in both eyes, with a
                       downshoot in full elevation

It had not looked and concluded bilateral. It had been told to expect bilateral
and agreed. `blind_disagreement` compares a description made without the
station against what was asked for, so agreement is evidence rather than
compliance.
"""

from __future__ import annotations

from app.models import OsceStation
from app.services.osce.station_images.verify import blind_disagreement


def test_a_bilateral_station_shown_one_affected_eye_is_flagged():
    station = OsceStation(
        diagnosis="Bilateral Brown's Syndrome",
        findings_elicited="limited elevation in adduction in both eyes",
    )
    note = blind_disagreement(
        {"modality": "external", "laterality": "both_eyes", "affected": "one_eye_affected"},
        None,
        station,
    )
    assert note and "only one is affected" in note


def test_a_bilateral_station_shown_both_affected_is_not_flagged():
    station = OsceStation(
        diagnosis="Bilateral Brown's Syndrome",
        findings_elicited="limited elevation in adduction in both eyes",
    )
    assert blind_disagreement(
        {"modality": "external", "laterality": "both_eyes", "affected": "both_eyes_affected"},
        None,
        station,
    ) is None


def test_a_unilateral_station_is_not_flagged_for_one_affected_eye():
    """The check must only fire where the station really did say both."""
    station = OsceStation(
        diagnosis="Right CN3 palsy",
        findings_elicited="ptosis and a down-and-out right eye",
    )
    assert blind_disagreement(
        {"modality": "external", "laterality": "both_eyes", "affected": "one_eye_affected"},
        None,
        station,
    ) is None


def test_the_wrong_modality_is_flagged_without_the_station_being_consulted():
    """Station 257 asked for a CT of the orbits and was given a head MRI."""
    station = OsceStation(diagnosis="Thyroid eye disease", findings_elicited="proptosis")
    note = blind_disagreement(
        {"modality": "fundus", "laterality": "one_eye", "affected": "one_eye_affected"},
        "CT scan of the orbits",
        station,
    )
    assert note and "fundus" in note


def test_a_missing_blind_description_is_not_a_disagreement():
    """The call is allowed to fail; losing a caption must not lose the image."""
    station = OsceStation(diagnosis="Anything", findings_elicited="both eyes affected")
    assert blind_disagreement({}, None, station) is None


def test_modality_is_not_judged_against_a_findings_blob():
    """A figure that named no view carries the station's whole findings text,
    and `expected_modalities_for` then guesses from whatever words are in it.

    Figure 19 wanted "a normal anterior segment and an optic nerve pigmented
    lesion" - which yields external/slit_lamp/topography and calls the correct
    fundus photograph wrong. 146 of the first sweep's 169 disagreements were
    this.
    """
    station = OsceStation(
        diagnosis="Optic disc melanocytoma",
        findings_elicited="normal anterior segment and an optic nerve pigmented lesion",
    )
    blind = {"modality": "fundus", "laterality": "one_eye", "affected": "one_eye_affected"}
    assert blind_disagreement(blind, None, station) is None
    # But a question that really did ask for a CT is still checked.
    assert blind_disagreement(blind, "CT scan of the orbits", station) is not None
