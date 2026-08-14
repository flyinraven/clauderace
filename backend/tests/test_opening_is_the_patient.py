"""The opening screen shows the patient, and only what is certainly the patient.

Station 312 opened on six figures: two photographs of the eye, an IOL
calculation printout, and specular microscopy of both eyes. All three
printouts were classified "other", and "other" was inside
PATIENT_VIEW_MODALITIES - so anything the vision model could not name was
treated as a view of the patient and handed over before the candidate had been
asked to examine anything.
"""

from __future__ import annotations

from app.services.imagesearch.relevance import is_investigation, is_the_patient


def test_a_named_patient_view_opens_the_station():
    for modality in ("external", "slit_lamp", "fundus"):
        assert is_the_patient(modality)


def test_an_investigation_never_does():
    for modality in ("oct", "topography", "biometry", "specular", "radiology"):
        assert not is_the_patient(modality)


def test_unclassified_is_not_the_patient():
    """The asymmetry is the point. Showing an investigation early hands over
    the answer; holding a patient photograph back is caught by the blank-screen
    fallback, which returns what was held rather than showing nothing."""
    assert not is_the_patient("other")
    assert not is_the_patient(None)
    assert not is_the_patient("")
    # `is_investigation` keeps its old, more forgiving answer: the two ask
    # different questions and only the opening screen needs the strict one.
    assert not is_investigation("other")


def test_a_station_of_nothing_but_printouts_still_shows_something():
    """Blank is worse than early."""
    from app.api.osce.helpers import opening_figures_payload

    class Fig:
        def __init__(self, i, modality):
            self.id, self.modality, self.position = i, modality, i
            self.image_id, self.is_approved = 100 + i, True
            self.caption = f"{modality} image"
            self.described_findings = None
            self.described_findings_approved = False

    class Station:
        prompts = []
        figures = [Fig(0, "other"), Fig(1, "biometry")]

    shown = opening_figures_payload(Station())
    assert len(shown) == 2, "a station whose every image is a printout must not go blank"
