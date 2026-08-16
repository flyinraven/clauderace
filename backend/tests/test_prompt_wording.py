"""Rewording a station's prompts by hand must not disturb what it marks.

The 2011 papers print the questions the real candidates were asked. Generated
wording is not a substitute: it reads the station's recorded findings as context
and hands them straight back in the question, which gives away every mark for
eliciting a sign. This endpoint is how the printed wording gets in without
spending a model call - so the thing to protect is that it changes the words and
nothing else.
"""

from __future__ import annotations

from app.constants import SOURCE_PAST_PAPER
from app.models import OsceStation
from tests.conftest import auth


def _station_with_prompts(db) -> OsceStation:
    station = OsceStation(
        source=SOURCE_PAST_PAPER,
        subspecialty="Cataract",
        diagnosis="Marfan's syndrome, atypical.",
        prompts=[
            {
                "label": "A",
                "text": "This patient's left eye is aphakic, with a patent peripheral "
                        "iridotomy. What do these findings tell you?",
                "marks": 6.5,
                "seconds": 180,
                "rubric": [{"text": "Identifies the aphakia", "marks": 2}],
                "figure_ids": [11],
            },
            {"label": "B", "text": "Generated second question.", "marks": 4, "seconds": 120},
        ],
        prompts_status="complete",
    )
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


def test_the_printed_wording_replaces_the_generated_wording(client, db, admin):
    station = _station_with_prompts(db)

    response = client.put(
        f"/api/osce/stations/{station.id}/prompts",
        json={"prompts": [{
            "label": "A",
            "text": "Please examine this patients anterior segments, posterior poles "
                    "and describe your findings",
        }]},
        headers=auth(admin),
    )

    assert response.status_code == 200
    assert response.json()["reworded"] == ["A"]
    db.refresh(station)
    assert station.prompts[0]["text"].startswith("Please examine this patients")


def test_marks_timing_rubric_and_figures_are_left_alone(client, db, admin):
    """The rubric is what a sitting is scored against; rewording must not move it."""
    station = _station_with_prompts(db)

    client.put(
        f"/api/osce/stations/{station.id}/prompts",
        json={"prompts": [{"label": "A", "text": "Please examine this patient."}]},
        headers=auth(admin),
    )

    db.refresh(station)
    first = station.prompts[0]
    assert first["marks"] == 6.5
    assert first["seconds"] == 180
    assert first["rubric"] == [{"text": "Identifies the aphakia", "marks": 2}]
    assert first["figure_ids"] == [11]


def test_prompts_not_named_in_the_request_are_untouched(client, db, admin):
    station = _station_with_prompts(db)

    client.put(
        f"/api/osce/stations/{station.id}/prompts",
        json={"prompts": [{"label": "A", "text": "Please examine this patient."}]},
        headers=auth(admin),
    )

    db.refresh(station)
    assert station.prompts[1]["text"] == "Generated second question."


def test_an_unknown_label_changes_nothing_at_all(client, db, admin):
    """A caller with the labels wrong has the mapping wrong. Half-applied
    rewording is harder to notice than none, so it is refused wholesale."""
    station = _station_with_prompts(db)
    before = station.prompts[0]["text"]

    response = client.put(
        f"/api/osce/stations/{station.id}/prompts",
        json={"prompts": [
            {"label": "A", "text": "Please examine this patient."},
            {"label": "Z", "text": "A question this station does not have."},
        ]},
        headers=auth(admin),
    )

    assert response.status_code == 400
    db.refresh(station)
    assert station.prompts[0]["text"] == before


def test_a_missing_station_is_a_404(client, admin):
    response = client.put(
        "/api/osce/stations/999999/prompts",
        json={"prompts": [{"label": "A", "text": "Please examine this patient."}]},
        headers=auth(admin),
    )

    assert response.status_code == 404
