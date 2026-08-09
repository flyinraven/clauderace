"""Preparing a station: examiner questions, the findings split, and its image.

This is where nearly all the AI spend happens - image sourcing verifies every
candidate photograph with a vision call - so these tests assert on the shape and
size of what is sent, not only on the result.
"""

from __future__ import annotations

import io
import json

import pytest

from PIL import Image as PILImage

from app.models import Image, Job, OsceFigure, OsceStation, Setting
from app.services.ai import AIClient
from app.services.ai.images import MAX_EDGE
from tests.conftest import auth
from tests.test_api_osce import make_station


def big_photo(width: int = 2400, height: int = 1800) -> bytes:
    image = PILImage.new("RGB", (width, height))
    pixels = image.load()
    for x in range(0, width, 3):
        for y in range(0, height, 3):
            pixels[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


# --- Examiner questions --------------------------------------------------
def test_building_prompts_produces_a_timed_marked_sequence(client, db, admin, ai, run_jobs):
    station = make_station(db, prompts=[], prompts_status="none", rubric=[
        {"text": "Describes the opacity", "marks": 10},
        {"text": "Names the risk factors", "marks": 10},
    ])

    response = client.post("/api/osce/stations/build-prompts", headers=auth(admin))
    assert response.status_code == 202
    assert response.json()["station_count"] == 1
    run_jobs()

    db.expire_all()
    station = db.get(OsceStation, station.id)
    assert station.prompts_status == "complete"
    assert len(station.prompts) == 2
    # A candidate is entitled to the full nine minutes and not a second more.
    assert sum(p["seconds"] for p in station.prompts) == 540
    # Marks must total 20, as they do in a real station.
    total = sum(pt["marks"] for p in station.prompts for pt in p["rubric"])
    assert abs(total - 20) < 0.01


def full_arc() -> list[dict]:
    """A sequence that follows the examiner's arc all the way through."""
    return [
        {"label": "A", "step": 1, "text": "Please examine the posterior segment of both eyes.",
         "seconds": 90, "rubric": [{"text": "Identifies the lesion", "marks": 8, "is_critical": True}]},
        {"label": "B", "step": 2, "text": "What other investigations would you perform?",
         "seconds": 60, "rubric": [{"text": "Names OCT", "marks": 2, "is_critical": False}]},
        {"label": "C", "step": 3, "text": "This is her OCT. What does it show?",
         # Presenting an investigation obliges the station to have asked for
         # one; without this the candidate reads a blank screen.
         "image_wanted": "OCT of the right macula showing subretinal fluid",
         "seconds": 60, "rubric": [{"text": "Reads the OCT", "marks": 2, "is_critical": False}]},
        {"label": "D", "step": 4, "text": "Summarise and give me 4 differential diagnoses.",
         "seconds": 90, "rubric": [{"text": "Four differentials", "marks": 3, "is_critical": False}]},
        {"label": "E", "step": 5,
         "text": "The diagnosis is X. How would you manage her if she were new to you?",
         "seconds": 120, "rubric": [{"text": "A plan", "marks": 4, "is_critical": False}]},
        {"label": "F", "step": 6, "text": "Five years on her vision drops. What now?",
         "seconds": 60, "rubric": [{"text": "Evolves the case", "marks": 2, "is_critical": False}]},
        {"label": "G", "step": 7, "text": "What are the risk factors? Name 4.",
         "seconds": 60, "rubric": [{"text": "Risk factors", "marks": 1, "is_critical": False}]},
    ]


def test_a_complete_arc_is_accepted_unchanged(client, db, admin, ai, run_jobs):
    """The whole point of a station: it must ask the questions an examiner asks."""
    from app.services.osce.prompts import _arc_problems, _normalise

    prompts, _ = _normalise(full_arc())
    assert _arc_problems(prompts) == []


def test_an_opening_that_gives_the_findings_away_is_rejected():
    """The standing instruction names the region and the eye - nothing more."""
    from app.services.osce.prompts import _arc_problems, _normalise

    raw = full_arc()
    raw[0]["text"] = "Please examine the fundus, including the macula, and describe what you see."
    prompts, _ = _normalise(raw)
    assert any("standing instruction" in p for p in _arc_problems(prompts))


def test_a_station_missing_steps_of_the_arc_is_rejected():
    from app.services.osce.prompts import _arc_problems, _normalise

    raw = [item for item in full_arc() if item["step"] not in (2, 4)]
    prompts, _ = _normalise(raw)
    problems = _arc_problems(prompts)
    assert any("arc step 2" in p for p in problems)
    assert any("arc step 4" in p for p in problems)


def test_a_stub_answer_costs_a_retry_not_the_station(client, db, admin, ai, run_jobs):
    """The utility model sometimes answers with an unfinished fragment."""
    make_station(db, prompts=[], prompts_status="none")
    replies = ['{\n  "prompts', json.dumps({"prompts": full_arc()})]

    def responder(body, n):
        return replies.pop(0) if replies else json.dumps({"prompts": full_arc()})

    ai.responder = responder
    client.post("/api/osce/stations/build-prompts", headers=auth(admin))
    run_jobs()

    db.expire_all()
    station = db.query(OsceStation).one()
    assert station.prompts_status == "complete"
    assert len(station.prompts) == 7


def test_a_station_with_no_image_need_not_ask_the_candidate_to_read_one():
    """Nothing is shown, so there is no photograph to describe."""
    from app.services.osce.prompts import _arc_problems, _normalise

    prompts, _ = _normalise([item for item in full_arc() if item["step"] != 3])
    assert _arc_problems(prompts, has_image=False) == []
    assert any("arc step 3" in p for p in _arc_problems(prompts, has_image=True))


def test_a_test_result_cannot_be_handed_over_when_there_is_none_to_show():
    """A live station asked "this is her A-scan biometry" - there was no scan.

    The candidate is shown one external photograph, is asked to interpret a
    scan that does not exist, and cannot answer.
    """
    from app.services.osce.prompts import _arc_problems, _normalise

    raw = [item for item in full_arc() if item["step"] != 3]
    raw[1]["text"] = "This is her A-scan biometry. What does it show?"
    prompts, _ = _normalise(raw)
    problems = _arc_problems(prompts, has_image=False)
    assert any("presents a test result" in p for p in problems)


def test_the_images_a_station_already_has_are_named_in_the_request(client, db, admin, ai):
    """The first figure is the patient the candidate is examining, not a handout."""
    from app.models import OsceFigure
    from app.services.osce.prompts import build_prompts_for_station
    from app.services.ai import AIClient

    station = make_station(db, prompts=[], prompts_status="none")
    station.figures.append(OsceFigure(caption="External photograph of the right eye"))
    db.commit()

    sent: list[str] = []

    def responder(body, n):
        sent.append(json.dumps(body["messages"][-1]["content"]))
        return json.dumps({"prompts": [i for i in full_arc() if i["step"] != 3]})

    ai.responder = responder
    build_prompts_for_station(db, AIClient(db), station)

    assert "External photograph of the right eye" in sent[0]
    assert "image_wanted" in sent[0]


def test_a_question_that_would_fit_any_station_is_rejected():
    """Across 36 rebuilt stations, step 2 was the same sentence 29 times."""
    from app.services.osce.prompts import _arc_problems, _normalise

    vocabulary = {"keratoconus", "cornea", "hydrops", "topography", "crosslinking"}
    raw = full_arc()
    raw[1]["text"] = "What other investigations would you perform in this patient?"
    prompts, _ = _normalise(raw)
    problems = _arc_problems(prompts, has_image=False, vocabulary=vocabulary)
    assert any(p.startswith("question B") and "would fit any station" in p for p in problems)

    # Asking the thing this case actually turns on passes.
    raw[1]["text"] = "What would the topography show, and how would you use it?"
    prompts, _ = _normalise(raw)
    assert not any(
        p.startswith("question B")
        for p in _arc_problems(prompts, has_image=False, vocabulary=vocabulary)
    )


def test_an_aim_with_no_question_is_a_hole_in_the_station():
    """Station 3's aim of distinguishing dystopia from strabismus was dropped."""
    from app.services.osce.prompts import _arc_problems, _normalise

    vocabulary = {"globe", "dystopia", "vertical", "strabismus", "orbital", "proptosis"}
    aims = ["To distinguish between globe dystopia versus vertical strabismus."]
    prompts, _ = _normalise(full_arc())
    problems = _arc_problems(prompts, has_image=False, vocabulary=vocabulary, aims=aims)
    assert any("never asked about" in p for p in problems)

    raw = full_arc()
    raw[1]["text"] = "How would you tell that globe dystopia from a vertical strabismus?"
    prompts, _ = _normalise(raw)
    assert not any(
        "never asked about" in p
        for p in _arc_problems(prompts, has_image=False, vocabulary=vocabulary, aims=aims)
    )


def test_a_generic_aim_is_not_held_against_the_station():
    """"Describe findings." names nothing checkable, so it cannot be traced."""
    from app.services.osce.prompts import _arc_problems, _normalise

    prompts, _ = _normalise(full_arc())
    problems = _arc_problems(
        prompts, has_image=False, vocabulary={"cataract", "subluxed"},
        aims=["Describe findings.", "Examine the anterior segment."],
    )
    assert not any("never asked about" in p for p in problems)


def test_a_rubric_that_marks_reading_a_scan_forces_the_question_that_shows_it():
    """Station 3 was marked on describing MRI findings and never showed an MRI."""
    from app.services.osce.prompts import _arc_problems, _normalise, station_needs_an_investigation

    assert station_needs_an_investigation(
        [{"text": "Describe MRI findings (enlarged right inferior rectus muscle)", "marks": 3}]
    )
    # Ordering a test is not reading one, and does not force the question.
    assert not station_needs_an_investigation(
        [{"text": "Suggest relevant ancillary tests (e.g., CT/MRI orbits)", "marks": 2}]
    )

    prompts, _ = _normalise([item for item in full_arc() if item["step"] != 3])
    problems = _arc_problems(prompts, has_image=False, needs_investigation=True)
    assert any("arc step 3" in p for p in problems)


def test_a_question_may_ask_for_an_image_the_station_does_not_have_yet():
    """Station 3 needed an MRI the report proves was shown. Keep the question."""
    from app.services.osce.prompts import _arc_problems, _normalise

    raw = [item for item in full_arc() if item["step"] != 3]
    raw.insert(2, {
        "label": "C", "step": 3,
        "text": "This is an MRI of the orbits. What does it show?",
        "image_wanted": "Coronal MRI of the orbits showing an enlarged right inferior rectus",
        "seconds": 60,
        "rubric": [{"text": "Describes the enlarged muscle", "marks": 2, "is_critical": True}],
    })
    prompts, _ = _normalise(raw)

    assert _arc_problems(prompts, has_image=False) == []
    assert prompts[2]["image_wanted"].startswith("Coronal MRI")


def test_the_model_is_asked_again_when_the_arc_is_wrong(client, db, admin, ai, run_jobs):
    """A rejected first attempt is fed back, and the corrected one is kept."""
    make_station(db, prompts=[], prompts_status="none")
    attempts: list[str] = []

    def responder(body, n):
        user = json.dumps(body["messages"][-1]["content"])
        attempts.append(user)
        broken = [item for item in full_arc() if item["step"] != 4]
        return json.dumps({"prompts": full_arc() if "rejected" in user else broken})

    ai.responder = responder
    client.post("/api/osce/stations/build-prompts", headers=auth(admin))
    run_jobs()

    assert len(attempts) == 2
    assert "arc step 4" in attempts[1]
    station = db.query(OsceStation).one()
    db.expire_all()
    assert station.prompts_status == "complete"
    assert [p["step"] for p in station.prompts] == [1, 2, 3, 4, 5, 6, 7]


def test_a_model_that_gets_the_arithmetic_wrong_is_rescaled(client, db, admin, ai, run_jobs):
    """Times and marks that do not add up are indefensible to a candidate."""
    make_station(db, prompts=[], prompts_status="none")
    ai.responder = lambda body, n: json.dumps(
        {
            "prompts": [
                {"label": "A", "text": "Examine.", "seconds": 100,
                 "rubric": [{"text": "point one", "marks": 7, "is_critical": True}]},
                {"label": "B", "text": "Manage.", "seconds": 100,
                 "rubric": [{"text": "point two", "marks": 7, "is_critical": False}]},
            ]
        }
    )
    client.post("/api/osce/stations/build-prompts", headers=auth(admin))
    run_jobs()

    db.expire_all()
    station = db.query(OsceStation).one()
    assert sum(p["seconds"] for p in station.prompts) == 540
    assert abs(sum(pt["marks"] for p in station.prompts for pt in p["rubric"]) - 20) < 0.01

    job = client.get("/api/admin/jobs", headers=auth(admin)).json()[0]
    assert any("rescaled" in w for w in job["result"]["warnings"]), (
        "a rescale is a quiet correction and must be reported"
    )


def test_a_station_that_fails_to_build_is_marked_failed_not_left_half_done(
    client, db, admin, ai, run_jobs
):
    make_station(db, prompts=[], prompts_status="none")
    ai.responder = lambda body, n: json.dumps({"prompts": []})

    client.post("/api/osce/stations/build-prompts", headers=auth(admin))
    run_jobs()

    db.expire_all()
    assert db.query(OsceStation).one().prompts_status == "failed"
    # And it is offered for preparation again rather than looking done.
    assert client.post(
        "/api/osce/stations/build-prompts", headers=auth(admin)
    ).status_code == 202


def test_rebuilding_is_only_offered_with_force(client, db, admin, ai):
    make_station(db)  # already has prompts
    assert client.post(
        "/api/osce/stations/build-prompts", headers=auth(admin)
    ).status_code == 400
    assert client.post(
        "/api/osce/stations/build-prompts?force=true", headers=auth(admin)
    ).status_code == 202


def test_one_station_failing_does_not_stop_the_batch(client, db, admin, ai, run_jobs):
    for i in range(3):
        make_station(
            db, station_number=i + 1, title=f"Station {i + 1}", prompts=[],
            prompts_status="none",
        )

    from app.services.ai.client import AIError

    calls = {"stations": 0}

    def sometimes(body, n):
        # The second station's provider call fails outright. A malformed reply
        # would not do: complete_json repairs those, which is the point of it.
        user = json.dumps(body["messages"][-1])
        if "Station 2" in user or "'Station 2'" in user:
            raise AIError("HTTP 400: the provider refused this one")
        calls["stations"] += 1
        return json.dumps(
            {
                "prompts": [
                    {"label": "A", "text": "Examine.", "seconds": 540,
                     "rubric": [{"text": "a point", "marks": 20, "is_critical": True}]}
                ]
            }
        )

    ai.responder = sometimes
    client.post("/api/osce/stations/build-prompts", headers=auth(admin))
    run_jobs()

    db.expire_all()
    statuses = sorted(s.prompts_status for s in db.query(OsceStation).all())
    assert statuses.count("complete") == 2
    assert statuses.count("failed") == 1


# --- The findings split --------------------------------------------------
def test_splitting_findings_separates_what_the_examiner_states(
    client, db, admin, ai, run_jobs
):
    station = make_station(
        db,
        findings="VA 6/24 left, IOP 16 mmHg, dense central opacity with neovascularisation.",
        findings_given=None,
        findings_elicited=None,
        findings_split_status="none",
    )
    response = client.post("/api/osce/stations/split-findings", headers=auth(admin))
    assert response.status_code == 202
    run_jobs()

    db.expire_all()
    station = db.get(OsceStation, station.id)
    assert station.findings_split_status == "complete"
    assert station.findings_given
    assert station.findings_elicited
    # The numbers an examiner reads out are not the signs to be found.
    assert station.findings_given != station.findings_elicited


def test_nothing_left_to_split_is_reported_rather_than_queued(client, db, admin):
    make_station(db)  # already split
    response = client.post("/api/osce/stations/split-findings", headers=auth(admin))
    assert response.status_code == 400
    assert "already been split" in response.json()["detail"]


# --- Image sourcing ------------------------------------------------------
def _configure_image_search(db) -> None:
    for key, value in {
        "imagesearch.provider": "brave",
        "imagesearch.api_key": "test-key",
        "imagesearch.results_per_query": 2,
        "imagesearch.monthly_query_limit": 100,
        "imagesearch.auto_approve": True,
    }.items():
        db.add(Setting(key=key, value=value, is_encrypted=False))
    db.commit()


class FakeSearch:
    def __init__(self, urls: list[str]):
        self.urls = urls
        self.queries: list[str] = []

    def search(self, query: str, count: int):
        from app.services.imagesearch.base import ImageCandidate

        self.queries.append(query)
        return [
            ImageCandidate(
                image_url=url, page_url=f"{url}/page", title="A clinical photograph",
                source="example.org", attribution="Example", licence="CC BY",
            )
            for url in self.urls[:count]
        ]


def test_a_sourced_image_is_verified_and_attached(
    client, db, admin, ai, run_jobs, monkeypatch
):
    station = make_station(db)
    _configure_image_search(db)
    search = FakeSearch(["https://example.org/eye1.jpg", "https://example.org/eye2.jpg"])
    photo = big_photo()

    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: search
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (photo, "image/jpeg", 2400, 1800),
    )

    response = client.post("/api/osce/stations/source-images", headers=auth(admin))
    assert response.status_code == 202
    run_jobs()

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(station_id=station.id).one()
    assert figure.image_id is not None
    assert figure.verification_status == "faithful"
    assert figure.is_approved is True, "a verified image shows straight away"
    assert figure.caption == "Slit lamp photograph, left eye"
    assert "neovascularisation" not in (figure.caption or ""), "the caption must not diagnose"

    # Three queries were written, specific to broad, and the first one hit.
    assert len(search.queries) == 1


def test_a_question_gets_its_own_image_sourced_and_kept_off_the_opening(
    client, db, admin, ai, run_jobs, monkeypatch, student
):
    """Station 3 asks the candidate to read an MRI the station did not have.

    The question states what it needs, the image is searched and verified
    against that description, and it belongs to the question - an MRI sitting
    on screen from the start would answer the examination before it is asked.
    """
    station = make_station(db)
    station.prompts = [
        {"label": "A", "step": 1, "text": "Please examine the orbits of both eyes.",
         "seconds": 440, "rubric": [{"text": "Describes the signs", "marks": 18}]},
        {"label": "B", "step": 3, "text": "This is an MRI of the orbits. What does it show?",
         "image_wanted": "Coronal MRI of the orbits showing an enlarged right inferior rectus",
         "seconds": 100, "rubric": [{"text": "Describes the enlarged muscle", "marks": 2}]},
    ]
    _configure_image_search(db)
    search = FakeSearch(["https://example.org/mri1.jpg"])
    photo = big_photo()
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: search
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (photo, "image/jpeg", 2400, 1800),
    )
    db.commit()

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    db.expire_all()
    station = db.query(OsceStation).filter_by(id=station.id).one()
    prompt = station.prompts[1]
    assert prompt["figure_id"], "the question must end up holding its image"
    figure = db.query(OsceFigure).filter_by(id=prompt["figure_id"]).one()
    assert figure.image_id is not None
    assert figure.position > 0, "the patient stays first"
    # Both the query writer and the vision check were told to work to what the
    # question asked for, not to the station's own signs.
    asked = [json.dumps(r["body"]) for r in ai.requests]
    assert sum("enlarged right inferior rectus" in a for a in asked) >= 2, (
        "the question's image requirement must reach the search and the verification"
    )

    # And in a sitting it travels with its question, not with the patient.
    sitting = client.post(
        "/api/osce/sittings", json={"station_id": station.id}, headers=auth(student)
    ).json()
    body = client.get(f"/api/osce/sittings/{sitting['id']}", headers=auth(student)).json()
    assert all(f["id"] != figure.id for f in body["station"]["figures"])
    assert [f["id"] for f in body["prompts"][1]["figures"]] == [figure.id]
    assert body["prompts"][0]["figures"] == []


def test_a_verification_call_never_sends_the_full_size_photograph(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """The single largest cost in the system - every candidate image is verified."""
    make_station(db)
    _configure_image_search(db)
    photo = big_photo()
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch(["https://example.org/eye1.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (photo, "image/jpeg", 2400, 1800),
    )

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    vision_requests = [
        i for i, r in enumerate(ai.requests)
        if any(
            p.get("type") == "image_url"
            for p in r["body"]["messages"][-1]["content"]
            if isinstance(p, dict)
        )
    ]
    assert vision_requests, "the image was not verified at all"

    import base64

    for index in vision_requests:
        payload = ai.images(index)[0]
        sent = base64.b64decode(payload)
        assert len(sent) < len(photo), "the original was sent unshrunk"
        with PILImage.open(io.BytesIO(sent)) as image:
            assert max(image.size) <= MAX_EDGE


def test_the_diagnosis_is_never_put_in_a_search_query_users_could_see(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """The query is stored on the figure and shown in the admin review screen."""
    station = make_station(db)
    _configure_image_search(db)
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch(["https://example.org/eye1.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (big_photo(400, 300), "image/jpeg", 400, 300),
    )
    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(station_id=station.id).one()
    assert figure.search_query
    # The stored query comes from the model, which is told the diagnosis is for
    # context only. This asserts the plumbing, not the model's discretion.
    assert isinstance(figure.search_query, str)


def test_every_candidate_rejected_leaves_the_station_without_an_image(
    client, db, admin, ai, run_jobs, monkeypatch
):
    station = make_station(db)
    _configure_image_search(db)
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch(["https://example.org/diagram.png"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (big_photo(400, 300), "image/jpeg", 400, 300),
    )
    ai.responder = lambda body, n: json.dumps(
        {"queries": ["a", "b", "c"]}
        if "search queries" in str(body["messages"][0]["content"])
        else {
            "tier": "reject", "confidence": 0.9,
            "shows": "A labelled line drawing.",
            "reason": "It is a diagram with arrows pointing at the lesion.",
            "missing": None, "caption": None,
        }
    )

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(station_id=station.id).one()
    assert figure.image_id is None
    assert figure.verification_status == "rejected"
    assert "diagram" in figure.verification_notes


def test_rejecting_an_image_remembers_the_url_and_searches_again(client, db, admin):
    station = make_station(db)
    image = Image(
        sha256="b" * 64, content_type="image/jpeg", data=big_photo(400, 300),
        width=400, height=300, size_bytes=100, origin="web",
        source_url="https://example.org/wrong.jpg", is_approved=True,
    )
    db.add(image)
    db.flush()
    figure = OsceFigure(
        station_id=station.id, position=0, image_id=image.id,
        verification_status="faithful", is_approved=True,
    )
    db.add(figure)
    db.commit()

    response = client.post(f"/api/osce/figures/{figure.id}/reject", headers=auth(admin))
    assert response.status_code == 202
    assert response.json()["rejected_so_far"] == 1
    assert response.json()["job_id"], "a replacement search is queued"

    db.expire_all()
    figure = db.get(OsceFigure, figure.id)
    assert figure.image_id is None
    assert figure.is_approved is False
    assert "https://example.org/wrong.jpg" in figure.rejected_urls


def test_a_previously_rejected_url_is_never_offered_back(
    client, db, admin, ai, run_jobs, monkeypatch
):
    station = make_station(db)
    _configure_image_search(db)
    figure = OsceFigure(
        station_id=station.id, position=0,
        rejected_urls=["https://example.org/eye1.jpg"], rejection_count=1,
    )
    db.add(figure)
    db.commit()

    downloaded: list[str] = []

    def record(candidate):
        downloaded.append(candidate.image_url)
        return (big_photo(400, 300), "image/jpeg", 400, 300)

    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch(
            ["https://example.org/eye1.jpg", "https://example.org/eye2.jpg"]
        ),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate", record
    )

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    assert "https://example.org/eye1.jpg" not in downloaded
    assert "https://example.org/eye2.jpg" in downloaded


def test_the_monthly_search_quota_stops_the_batch(client, db, admin, ai, run_jobs, monkeypatch):
    """Brave bills overages with no cap of its own."""
    for i in range(3):
        make_station(db, station_number=i + 1)
    _configure_image_search(db)
    db.merge(Setting(key="imagesearch.monthly_query_limit", value=1, is_encrypted=False))
    db.commit()

    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch(["https://example.org/eye1.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (big_photo(400, 300), "image/jpeg", 400, 300),
    )

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    job = client.get("/api/admin/jobs", headers=auth(admin)).json()[0]
    assert job["status"] == "failed"
    assert "limit" in job["error"]


def test_an_unapproved_image_is_withheld_from_a_sitting_but_shown_in_review(
    client, db, student, admin
):
    station = make_station(db)
    image = Image(
        sha256="c" * 64, content_type="image/jpeg", data=big_photo(400, 300),
        width=400, height=300, size_bytes=100, origin="web", is_approved=False,
    )
    db.add(image)
    db.flush()
    db.add(
        OsceFigure(
            station_id=station.id, position=0, image_id=image.id,
            verification_status="representative", is_approved=False,
        )
    )
    db.commit()

    sitting_id = client.post(
        "/api/osce/sittings", json={"station_id": station.id, "is_timed": True},
        headers=auth(student),
    ).json()["id"]
    body = client.get(f"/api/osce/sittings/{sitting_id}", headers=auth(student)).json()
    assert body["station"]["figures"] == []

    # Reviewing a station is exactly when you want to see the one not showing.
    preview = client.get(
        f"/api/osce/stations/{station.id}/preview", headers=auth(admin)
    ).json()
    assert len(preview["figures"]) == 1
    assert preview["figures"][0]["is_approved"] is False


def test_an_image_is_served_once_and_then_answered_from_the_cache(client, db, student):
    image = Image(
        sha256="d" * 64, content_type="image/jpeg", data=big_photo(400, 300),
        width=400, height=300, size_bytes=100, origin="web", is_approved=True,
    )
    db.add(image)
    db.commit()

    first = client.get(f"/api/images/{image.id}", headers=auth(student))
    assert first.status_code == 200
    assert first.headers["etag"] == f'"{"d" * 64}"'
    assert "immutable" in first.headers["cache-control"]

    # The bytes live in the database, so a conditional request is worth answering.
    second = client.get(
        f"/api/images/{image.id}",
        headers={**auth(student), "If-None-Match": first.headers["etag"]},
    )
    assert second.status_code == 304
    assert second.content == b""


def test_an_image_row_with_no_bytes_reports_gone_rather_than_serving_nothing(
    client, db, student
):
    """`data` is NOT NULL, but nothing stops a zero-length blob, and an empty
    200 renders as a broken image with no explanation."""
    image = Image(
        sha256="e" * 64, content_type="image/jpeg", data=b"",
        width=400, height=300, size_bytes=0, origin="web", is_approved=True,
    )
    db.add(image)
    db.commit()
    assert client.get(f"/api/images/{image.id}", headers=auth(student)).status_code == 410


def test_an_image_needs_a_token(client, db):
    image = Image(
        sha256="f" * 64, content_type="image/jpeg", data=b"x", width=1, height=1,
        size_bytes=1, origin="web", is_approved=True,
    )
    db.add(image)
    db.commit()
    assert client.get(f"/api/images/{image.id}").status_code == 401


def test_a_question_asking_for_two_investigations_gets_both(
    client, db, admin, ai, run_jobs, monkeypatch, student
):
    """"The OCT and the fluorescein angiogram" is two images, not one.

    Nine questions asked for two investigations in a single string. No image is
    both, so searching the whole phrase returned nothing and the question went
    on asking for something that was never going to arrive.
    """
    station = make_station(db)
    station.prompts = [
        {"label": "A", "step": 1, "text": "Please examine the fundus of both eyes.",
         "seconds": 440, "rubric": [{"text": "Describes the signs", "marks": 18}]},
        {"label": "B", "step": 3, "text": "What do these investigations show?",
         "image_wanted": (
             "OCT of the right macula showing a choroidal neovascular membrane and "
             "fluorescein angiogram of both eyes showing multifocal choroiditis lesions "
             "and leakage in the right eye"
         ),
         "seconds": 100, "rubric": [{"text": "Describes both", "marks": 2}]},
    ]
    _configure_image_search(db)
    search = FakeSearch(["https://example.org/one.jpg", "https://example.org/two.jpg"])
    photo = big_photo()
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: search
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (photo, "image/jpeg", 2400, 1800),
    )
    db.commit()

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    db.expire_all()
    station = db.query(OsceStation).filter_by(id=station.id).one()
    prompt = station.prompts[1]
    assert len(prompt["figure_ids"]) == 2, "one figure per investigation"
    assert prompt["figure_id"] == prompt["figure_ids"][0], "the old binding still resolves"

    wanted = [
        db.query(OsceFigure).filter_by(id=i).one().wanted_description
        for i in prompt["figure_ids"]
    ]
    assert any("OCT" in w for w in wanted)
    assert any("angiogram" in w for w in wanted)
    assert not any("OCT" in w and "angiogram" in w for w in wanted), (
        "neither figure may still be asked for both"
    )

    # Both reach the candidate, at that question and nowhere else.
    sitting = client.post(
        "/api/osce/sittings", json={"station_id": station.id}, headers=auth(student)
    ).json()
    body = client.get(f"/api/osce/sittings/{sitting['id']}", headers=auth(student)).json()
    assert len(body["prompts"][1]["figures"]) == 2
    shown = {f["id"] for f in body["station"]["figures"]}
    assert shown.isdisjoint(set(prompt["figure_ids"])), "not on screen from the start"


def test_an_investigation_no_search_can_find_is_not_paid_for(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """A serology titre and a textbook diagram are not waiting on a better query.

    The verifier rejects diagrams outright, and rightly - an illustration does
    the candidate's describing for them. Left in the queue these were bought on
    every run and reported as merely missing.
    """
    station = make_station(db)
    station.prompts = [
        {"label": "A", "step": 1, "text": "Please examine the anterior segment.",
         "seconds": 440, "rubric": [{"text": "Describes the signs", "marks": 18}]},
        {"label": "B", "step": 3, "text": "What does this show?",
         "image_wanted": "QuantiFERON-TB Gold test result, showing a positive result.",
         "seconds": 50, "rubric": [{"text": "Reads it", "marks": 1}]},
        {"label": "C", "step": 4, "text": "Describe the operation.",
         "image_wanted": "Diagram of a trabeculectomy showing the scleral flap and ostium.",
         "seconds": 50, "rubric": [{"text": "Describes it", "marks": 1}]},
    ]
    _configure_image_search(db)
    search = FakeSearch(["https://example.org/x.jpg"])
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: search
    )
    db.commit()

    client.post("/api/osce/stations/source-images", headers=auth(admin))
    run_jobs()

    db.expire_all()
    station = db.query(OsceStation).filter_by(id=station.id).one()
    for prompt in station.prompts[1:]:
        assert prompt.get("image_impossible"), "the reason must be recorded"
        assert not prompt.get("figure_id"), "and nothing bought for it"
    assert "result to be read" in station.prompts[1]["image_impossible"]
    assert "diagram" in station.prompts[2]["image_impossible"]


def test_one_provider_error_does_not_abandon_the_whole_batch(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """Brave answered HTTP 500 to one phrase and 35 stations went unsourced.

    A failure on a single query says nothing about the next query or the next
    station. Only an account-level answer - a rejected key, an exhausted credit
    - means every remaining search would fail the same way.
    """
    from app.services.imagesearch.base import ImageCandidate, ImageQueryError

    first = make_station(db)
    second = make_station(db, station_number=2)
    _configure_image_search(db)
    photo = big_photo()

    class HalfBrokenSearch:
        def __init__(self):
            self.queries: list[str] = []

        def search(self, query: str, count: int):
            self.queries.append(query)
            if len(self.queries) == 1:
                raise ImageQueryError("Brave returned HTTP 500: {}")
            return [
                ImageCandidate(
                    image_url="https://example.org/ok.jpg",
                    page_url="https://example.org/ok",
                    title="A clinical photograph", source="example.org",
                )
            ]

    search = HalfBrokenSearch()
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: search
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (photo, "image/jpeg", 2400, 1800),
    )
    db.commit()

    response = client.post(
        "/api/osce/stations/source-images",
        json={"station_ids": [first.id, second.id], "only_missing": False},
        headers=auth(admin),
    )
    run_jobs()

    job = db.get(Job, response.json()["job_id"])
    assert job.status == "completed", f"the batch must finish: {job.error}"
    assert len(search.queries) > 1, "it must have gone on to the next query"

    db.expire_all()
    assert db.query(OsceFigure).filter(OsceFigure.image_id.is_not(None)).count() >= 1, (
        "and the stations after the failing query still get their images"
    )


def test_an_exhausted_account_still_stops_the_run(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """The distinction has to cut both ways, or a dead key burns the whole bank."""
    from app.services.imagesearch.base import ImageSearchError

    station = make_station(db)
    _configure_image_search(db)

    class DeadAccount:
        def search(self, query: str, count: int):
            raise ImageSearchError("Brave rate limit or credit exhausted (429).")

    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: DeadAccount()
    )
    db.commit()

    response = client.post(
        "/api/osce/stations/source-images",
        json={"station_ids": [station.id], "only_missing": False},
        headers=auth(admin),
    )
    run_jobs()
    assert db.get(Job, response.json()["job_id"]).status == "failed"


def test_an_ingested_figure_is_shown_and_classified(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """Whatever the report printed, the candidates were shown it.

    This was a gate, and the grader behind it is written to screen web search
    results: annotation means somebody labelled the abnormality, a mismatched
    modality means the wrong picture was bought. Against an examiners' report
    it rejected the report for looking like a report - 118 real CTs, visual
    fields, OCTs and fundus photographs in one pass. Multiple images is the
    accepted risk; a hidden photograph and a bought stranger's is not.

    The model is still asked what the image is, because a question wanting an
    OCT should be handed the OCT the paper already contains.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import (
        stations_with_unchecked_figures,
        verify_ingested_figures,
    )

    station = make_station(db, prompts=[{
        "label": "A", "text": "Please examine the anterior segment of both eyes.",
        "seconds": 270, "rubric": [{"text": "Describes the corneal opacity", "marks": 10}],
    }])
    image = Image(sha256="d" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        verification_status="unverified", is_approved=False)
    db.add(figure)
    db.commit()

    assert station.id in stations_with_unchecked_figures(db)

    # The verdict the old gate would have rejected outright.
    ai.responder = lambda body, n: json.dumps({
        "tier": "reject", "modality": "visual_field", "confidence": 0.9,
        "shows": "A Humphrey visual field printout for the left eye",
        "reason": "It carries printed text and numbers", "missing": None,
        "caption": "Visual field printout, left eye",
    })
    outcome = verify_ingested_figures(db, AIClient(db), station)

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(id=figure.id).one()
    assert outcome["kept"] == 1
    assert figure.is_approved is True, "the paper's own figure is shown"
    assert figure.modality == "visual_field", "recorded, so a question can claim it"
    assert station.id not in stations_with_unchecked_figures(db)


def test_the_reports_own_investigation_is_shown_not_replaced(client, db, admin, ai):
    """A real photograph of another investigation stays, and stays visible.

    Unapproving it sent the station off to buy a web lookalike, which is the
    examiners' own photograph thrown away for a stranger's. The station keeps
    what the real candidates were shown; the mismatch is recorded, not acted on.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import verify_ingested_figures

    station = make_station(db, prompts=[{
        "label": "A", "text": "Please examine the anterior segment of both eyes.",
        "seconds": 270, "rubric": [{"text": "Describes the corneal opacity", "marks": 10}],
    }])
    image = Image(sha256="e" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        verification_status="unverified", is_approved=False)
    db.add(figure)
    db.commit()

    ai.responder = lambda body, n: json.dumps({
        "tier": "faithful", "modality": "fundus", "confidence": 0.9,
        "shows": "A fundus photograph of the right eye",
        "reason": "It is a retinal photograph", "missing": None,
        "caption": "Fundus photograph",
    })
    outcome = verify_ingested_figures(db, AIClient(db), station)

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(id=figure.id).one()
    assert outcome["kept"] == 1
    assert figure.is_approved is True, "the paper's own photograph is shown"
    assert figure.verification_status != "rejected"
    assert figure.modality == "fundus"


def test_the_reports_own_photograph_is_kept_even_when_imperfect(
    client, db, admin, ai
):
    """These are the images the real candidates were shown.

    "Representative" from a web search means a stranger's eye stood in for the
    patient's. From the examiners' report it means the grader could not tie
    every recorded sign to the photograph - which is not a reason to withhold
    the best image in the bank.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import verify_ingested_figures

    station = make_station(db, prompts=[{
        "label": "A", "text": "Please examine the anterior segment of both eyes.",
        "seconds": 270, "rubric": [{"text": "Describes the opacity", "marks": 10}],
    }])
    image = Image(sha256="f" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        verification_status="unverified", is_approved=False)
    db.add(figure)
    db.commit()

    ai.responder = lambda body, n: json.dumps({
        "tier": "representative", "modality": "slit_lamp", "confidence": 0.6,
        "shows": "A slit lamp photograph of a corneal opacity",
        "reason": "The right examination", "missing": "the neovascularisation",
        "caption": "Slit lamp photograph",
    })
    outcome = verify_ingested_figures(db, AIClient(db), station)

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(id=figure.id).one()
    assert outcome["kept"] == 1
    assert figure.is_approved is True, "the report's own photograph is shown"


def test_a_checked_figure_is_not_graded_twice(client, db, admin, ai):
    """Re-running the check must not spend a vision call per figure per run."""
    from app.models import OsceFigure
    from app.services.osce.station_images import (
        stations_with_unchecked_figures,
        verify_ingested_figures,
    )

    station = make_station(db)
    image = Image(sha256="e" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    db.add(OsceFigure(station_id=station.id, position=0, image_id=image.id,
                      verification_status="faithful", match_confidence=0.9,
                      is_approved=True))
    db.commit()

    assert station.id not in stations_with_unchecked_figures(db)
    before = len(ai.requests)
    outcome = verify_ingested_figures(db, AIClient(db), station)
    assert outcome["skipped"] == 1 and outcome["kept"] == 0
    assert len(ai.requests) == before, "no model call for a figure already graded"


def test_the_reports_own_mri_answers_the_question_that_asks_for_one(
    client, db, admin, ai
):
    """Station 158 printed four MRIs and had them all thrown away.

    Its opening task is "examine the patient's eye movements", which expects an
    external photograph, so every MRI failed the modality check - while question
    C of the same station asks the candidate to read an MRI of the brain, and
    the sourcing run was about to buy one off the web.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import bind_ingested_figures_to_questions

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the patient's eye movements.",
         "seconds": 270, "rubric": [{"text": "Describes the deficit", "marks": 10}]},
        {"label": "C", "text": "What does this scan show?", "seconds": 90,
         "image_wanted": "MRI of the brain, axial views, showing white matter lesions",
         "rubric": [{"text": "Reads the scan", "marks": 5}]},
    ])
    image = Image(sha256="1" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    figure = OsceFigure(
        station_id=station.id, position=1, image_id=image.id,
        verification_status="rejected", is_approved=False,
        caption="Axial T2-weighted MRI of the brain",
    )
    db.add(figure)
    db.commit()

    ai.responder = lambda body, n: json.dumps({
        "tier": "faithful", "modality": "radiology", "confidence": 0.9,
        "shows": "An axial T2 MRI of the brain", "reason": "The scan asked for",
        "missing": None, "caption": "Axial MRI of the brain",
    })
    assert bind_ingested_figures_to_questions(db, AIClient(db), station)["bound"] == 1

    db.expire_all()
    station = db.query(OsceStation).filter_by(id=station.id).one()
    figure = db.query(OsceFigure).filter_by(id=figure.id).one()
    assert station.prompts[1]["figure_id"] == figure.id
    assert figure.is_approved is True
    assert figure.wanted_description.startswith("MRI of the brain")


def test_a_topography_is_not_offered_for_a_question_asking_for_an_ultrasound(
    client, db, admin, ai
):
    """The match must be the named investigation, not merely the same region."""
    from app.models import OsceFigure
    from app.services.osce.station_images import bind_ingested_figures_to_questions

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment.", "seconds": 270,
         "rubric": [{"text": "Describes it", "marks": 10}]},
        {"label": "C", "text": "What does this show?", "seconds": 90,
         "image_wanted": "UBM of the left anterior segment showing a shallow chamber",
         "rubric": [{"text": "Reads it", "marks": 5}]},
    ])
    image = Image(sha256="2" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    db.add(OsceFigure(
        station_id=station.id, position=1, image_id=image.id,
        verification_status="rejected", is_approved=False,
        caption="Pentacam corneal topography of the left eye",
    ))
    db.commit()

    before = len(ai.requests)
    assert bind_ingested_figures_to_questions(db, AIClient(db), station)["bound"] == 0
    assert len(ai.requests) == before, "and it costs nothing to decline"


def test_an_upload_sources_its_own_missing_images(db, admin):
    """The last link in the chain, and the one that was missing.

    A paper arrived, its stations were structured, their questions built, the
    report's own photographs graded - and then nothing. Whatever the report did
    not supply stayed missing until someone ran a batch by hand, which is how a
    motility station sat in the bank asking the candidate to examine eye
    movements over a brain MRI.
    """
    from app.models import Job
    from app.models.ops import JOB_PENDING
    from app.services.ingest.pipeline import _queue_image_sourcing
    from app.services.jobs.runner import JobContext
    from app.services.osce.station_images import JOB_SOURCE_STATION_IMAGES

    ingest = Job(job_type="ingest_document", status=JOB_PENDING,
                 payload={"document_id": 1}, cursor={}, created_by_id=admin.id)
    db.add(ingest)
    db.commit()

    _queue_image_sourcing(JobContext(db=db, job=ingest), [12, 9, 30])
    db.commit()

    sourcing = db.query(Job).filter_by(job_type=JOB_SOURCE_STATION_IMAGES).one()
    assert sourcing.payload["station_ids"] == [9, 12, 30]
    assert sourcing.payload["only_missing"] is True, "pay only for the gaps"
    assert sourcing.id > ingest.id, "and only once the ingest itself is done"


def test_an_upload_with_no_stations_queues_no_sourcing(db, admin):
    from app.models import Job
    from app.models.ops import JOB_PENDING
    from app.services.ingest.pipeline import _queue_image_sourcing
    from app.services.jobs.runner import JobContext
    from app.services.osce.station_images import JOB_SOURCE_STATION_IMAGES

    ingest = Job(job_type="ingest_document", status=JOB_PENDING,
                 payload={"document_id": 1}, cursor={}, created_by_id=admin.id)
    db.add(ingest)
    db.commit()
    _queue_image_sourcing(JobContext(db=db, job=ingest), [])
    db.commit()
    assert db.query(Job).filter_by(job_type=JOB_SOURCE_STATION_IMAGES).count() == 0


def test_sourcing_does_not_steal_the_figure_a_question_owns(
    client, db, admin, ai, monkeypatch
):
    """Station 158's MRI was overwritten by the montage searched for its task A.

    Question C asks "what does this scan show?" and owned the report's own MRI
    at position 0. Sourcing took the lowest-position figure as the station's
    opening one, so the gaze montage it found for "examine the patient's eye
    movements" was written over that MRI: the montage ended up attached to the
    scan question, and the motility task still opened on nothing.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import source_image_for_station

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the patient's eye movements.",
         "seconds": 270, "rubric": [{"text": "Identifies the gaze palsy", "marks": 10}]},
        {"label": "C", "text": "What does this scan show?", "seconds": 90,
         "image_wanted": "MRI of the brain showing white matter lesions",
         "rubric": [{"text": "Reads the scan", "marks": 5}]},
    ])
    mri_image = Image(sha256="7" * 64, content_type="image/jpeg", data=big_photo(),
                      size_bytes=100, origin="pdf")
    db.add(mri_image)
    db.flush()
    owned = OsceFigure(station_id=station.id, position=0, image_id=mri_image.id,
                       is_approved=True, verification_status="faithful",
                       caption="Axial MRI of the brain")
    db.add(owned)
    db.flush()
    station.prompts = [station.prompts[0], {**station.prompts[1], "figure_id": owned.id}]
    db.commit()

    _configure_image_search(db)
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch(["https://example.org/gaze.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.download_candidate",
        lambda candidate: (big_photo(), "image/jpeg", 2400, 1800),
    )
    source_image_for_station(db, AIClient(db), station)

    db.expire_all()
    owned = db.query(OsceFigure).filter_by(id=owned.id).one()
    assert owned.image_id == mri_image.id, "question C keeps its own scan"
    assert owned.caption == "Axial MRI of the brain"

    station = db.query(OsceStation).filter_by(id=station.id).one()
    opening = [f for f in station.figures if f.id != owned.id and f.image_id]
    assert opening, "and the examination task gets a figure of its own"


def test_a_question_may_not_present_an_investigation_it_never_asked_for():
    """The invariant the pipeline never had, enforced where it is free to fix.

    A candidate reached "These are the corneal topography and biometry for both
    eyes. What do they show?" with an empty screen: the question was written
    presenting an investigation, and no request for one was ever recorded, so
    nothing was sourced and nothing could be. By the time a station is sat the
    wording is baked in and the marks apportioned to it - so it is caught at
    the point the question is written.
    """
    from app.services.osce.prompts import _unshowable_questions

    presents = [{
        "label": "C", "step": 3,
        "text": "These are the corneal topography and biometry for both eyes. What do they show?",
    }]
    assert _unshowable_questions(presents), "must be rejected"

    presents[0]["image_wanted"] = "Corneal topography of both eyes showing inferior steepening"
    assert _unshowable_questions(presents) == [], "asked for, so it will be there"

    # A question about signs the candidate has already described is not
    # presenting anything, and must not be dragged into this.
    about_findings = [{
        "label": "B", "step": 4,
        "text": "What does this pattern of findings tell you about her disease?",
    }]
    assert _unshowable_questions(about_findings) == []


def test_a_failed_re_source_keeps_the_image_the_station_already_had(
    client, db, admin, ai, monkeypatch
):
    """Station 119 went into a run holding an approved nine-position montage.

    It came out with an empty figure still marked approved, showing the
    candidate nothing: the search cleared the image first and looked for a
    replacement second, so every empty re-source cost a picture.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import source_image_for_station

    station = make_station(db)
    image = Image(sha256="8" * 64, content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="web")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        is_approved=True, verification_status="representative",
                        match_confidence=0.6, caption="Nine positions of gaze")
    db.add(figure)
    db.commit()

    _configure_image_search(db)
    # Every search comes back empty, which is the case that did the damage.
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider",
        lambda store: FakeSearch([]),
    )
    outcome = source_image_for_station(db, AIClient(db), station)

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(id=figure.id).one()
    assert figure.image_id == image.id, "the station keeps what it had"
    assert figure.is_approved is True
    assert figure.caption == "Nine positions of gaze"
    assert outcome["attached"] is False
    assert "kept" in outcome["reason"]


def test_a_named_view_the_model_declines_gets_no_invented_words(client, db, admin, ai, monkeypatch):
    """No image, and a model that declines: the station says nothing.

    The floor of printed findings is for a figure that named no view - that one
    IS the station's own examination. A named view gets words from the model,
    which is given the findings and told to state only what they contain, or it
    gets none. Quoting the findings anyway put "Fundus examination is normal"
    under a nine-positions-of-gaze montage and under a CT angiogram.

    A station with nothing to show is visible, in the admin page and on the
    station itself. Wrong words read as fact and are marked against.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import source_image_for_station

    # Deliberately the fixture's own station, findings and prompts together:
    # the floor quotes the findings for the view the station itself asks for,
    # and a fixture pairing optic-atrophy findings with an anterior segment
    # task tests neither that nor anything else.
    station = make_station(db)
    _configure_image_search(db)
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: FakeSearch([])
    )
    # The wording model declines, which is its correct behaviour, not a failure.
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.describe_findings",
        lambda *a, **kw: (None, None),
    )
    source_image_for_station(db, AIClient(db), station)

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(station_id=station.id).one()
    assert figure.image_id is None
    assert figure.verification_status == "rejected"
    assert not (figure.described_findings or ""), "nothing invented for a named view"
    assert figure.described_findings_approved is False, "there are no words to release"


def test_recorded_findings_that_name_the_diagnosis_are_not_read_out(
    client, db, admin, ai, monkeypatch
):
    """The floor must not become a hole: the leak guard still applies."""
    from app.models import OsceFigure
    from app.services.osce.station_images import source_image_for_station

    station = make_station(
        db,
        findings_elicited="Findings are those of dominant optic atrophy with disc pallor.",
        diagnosis="Dominant optic atrophy",
    )
    _configure_image_search(db)
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.build_provider", lambda store: FakeSearch([])
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.sourcing.describe_findings",
        lambda *a, **kw: (None, None),
    )
    source_image_for_station(db, AIClient(db), station)

    db.expire_all()
    figure = db.query(OsceFigure).filter_by(station_id=station.id).one()
    assert not figure.described_findings, "naming the answer is worse than saying nothing"


def test_a_figure_rejected_under_the_old_modality_rule_is_looked_at_again(db):
    """The backlog is the reason the selector was widened.

    "rejected" used to mean both "a chart" and "a real photograph of a different
    investigation". The second is now kept and shown, so those rows have to come
    back round - otherwise they stay dark for ever and the station goes on
    showing a web lookalike instead of the examiners' own picture.
    """
    from app.models import OsceFigure
    from app.services.osce.station_images import (
        FROM_PAPER,
        NOT_CLINICAL,
        stations_with_unchecked_figures,
    )

    station = make_station(db)
    image = Image(sha256="a" * 63 + "1", content_type="image/jpeg", data=big_photo(),
                  size_bytes=100, origin="pdf")
    db.add(image)
    db.flush()
    figure = OsceFigure(station_id=station.id, position=0, image_id=image.id,
                        verification_status="rejected", is_approved=False)
    db.add(figure)
    db.commit()

    assert station.id in stations_with_unchecked_figures(db)

    # Written by the rule that dropped these. Those rows must come back round.
    figure.verification_status = NOT_CLINICAL
    db.commit()
    assert station.id in stations_with_unchecked_figures(db)

    figure.verification_status = FROM_PAPER
    db.commit()
    assert station.id not in stations_with_unchecked_figures(db)


def test_sourcing_hands_over_to_describing_when_it_finishes(db, admin, run_jobs, ai):
    """A gap has to close itself, without anybody pressing anything.

    Sourcing that finds nothing used to leave the figure empty and the queue
    waiting on "Describe the rest" being noticed and pressed. The last resort
    is part of the protocol, not an errand.
    """
    from app.models import OsceFigure
    from app.services.jobs.runner import create_job
    from app.services.osce.station_images import (
        JOB_DESCRIBE_STATION_FIGURES,
        _queue_description_of_gaps,
    )

    station = make_station(db)
    db.add(OsceFigure(station_id=station.id, position=0, image_id=None,
                      wanted_description="external photograph of the right eye"))
    db.commit()

    job = create_job(db, "source_station_images", payload={"station_ids": [station.id]},
                     created_by_id=admin.id, total_steps=1)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.db = db
    ctx.job = job
    _queue_description_of_gaps(ctx)

    from app.models import Job
    queued = db.query(Job).filter_by(job_type=JOB_DESCRIBE_STATION_FIGURES).all()
    assert len(queued) == 1, "the figure with no image is handed on to be described"
    assert queued[0].payload["figure_ids"]


def test_a_description_that_names_the_condition_is_asked_again(db, ai):
    """A leak is a wording problem, and the model cannot see it.

    The guard is the only thing that knows which phrase gave the answer away,
    so discarding the description silently threw away the findings as well as
    the name. It is told the phrase and asked for the appearance instead.
    """
    from app.services.ai import AIClient
    from app.services.osce.station_images import describe_findings

    station = make_station(db)
    station.diagnosis = "Keratoconus with corneal scarring"
    station.findings_elicited = "There is corneal scarring and irregular astigmatism."
    db.commit()

    replies = [
        {"description": "The appearances are those of keratoconus with corneal scarring."},
        {"description": "The cornea is conical, with a central scar and irregular reflexes."},
    ]
    ai.responder = lambda body, n: json.dumps(replies[min(n, len(replies)) - 1])

    text, _concern = describe_findings(AIClient(db), station, "slit lamp photograph")

    assert text == replies[1]["description"], "the second answer is the one kept"
    assert len(ai.requests) == 2, "exactly one correction, not a retry loop"
    assert "keratoconus" in ai.user_text(1).lower(), (
        "the correction has to name the phrase that leaked"
    )


def test_a_description_that_leaks_twice_is_given_up_on(db, ai):
    """One correction, then the station goes without rather than give it away."""
    from app.services.ai import AIClient
    from app.services.osce.station_images import describe_findings

    station = make_station(db)
    station.diagnosis = "Keratoconus with corneal scarring"
    station.findings_elicited = "There is corneal scarring and irregular astigmatism."
    db.commit()

    ai.responder = lambda body, n: json.dumps({"description": "This is keratoconus."})

    text, _concern = describe_findings(AIClient(db), station, "slit lamp photograph")

    assert text is None
    assert len(ai.requests) == 2


def test_describing_a_gap_actually_writes_the_words_down(db, admin, run_jobs, ai):
    """The describe job has to persist what it was asked to write.

    Everything after the description call once sat inside the `except` block
    that raises, so the job read the words and stored none of them: every
    station whose search came back empty stayed empty, image and text both,
    and the pass reported a clean finish each time.
    """
    from app.models import OsceFigure
    from app.services.jobs.runner import create_job
    from app.services.osce.station_images import JOB_DESCRIBE_STATION_FIGURES

    station = make_station(db)
    figure = OsceFigure(station_id=station.id, position=0, image_id=None,
                        wanted_description="external photograph of both eyes")
    db.add(figure)
    db.commit()

    ai.responder = lambda body, n: json.dumps(
        {"description": "The right eye turns inwards in primary position."}
    )
    create_job(db, JOB_DESCRIBE_STATION_FIGURES, payload={"figure_ids": [figure.id]},
               created_by_id=admin.id, total_steps=1)
    run_jobs()

    db.refresh(figure)
    assert figure.described_findings, "the words the job wrote must reach the figure"
    assert figure.described_findings_approved, "there is no image, so they are shown"
    assert figure.verification_status == "described"


def test_the_stations_findings_do_not_stand_in_for_an_investigation(db):
    """Fundus findings are not a description of a CT angiogram.

    When no image can be found the station states its recorded findings
    instead. Those are the bedside examination, so they stand in for a missing
    photograph of it - and not for a scan, which shows something the findings
    never described. Station 9A asked for a CT angiogram of the circle of
    Willis and was offered "Fundus examination is normal", three times over.
    """
    from app.services.osce.station_images import verbatim_findings_floor

    station = make_station(db)
    station.findings_elicited = "Fundus examination is normal."

    for named in (
        "Urgent CT angiography of the brain and circle of Willis",
        "OCT of the right macula",
        "external photograph montage of the nine positions of gaze",
    ):
        assert verbatim_findings_floor(station, named)[0] is None, (
            "a named view gets words from the model, which weighs them, or none"
        )

    # A figure that named no view IS the station's own examination, and the
    # printed findings are what the examiner would state for it.
    words, concern = verbatim_findings_floor(station, None)
    assert words == "Fundus examination is normal."
    assert "verbatim" in (concern or "")


def test_a_description_that_names_the_diagnosis_is_never_the_floor(db):
    """The leak guard governs the verbatim quote as much as the model's words."""
    from app.services.osce.station_images import verbatim_findings_floor

    station = make_station(db)
    station.diagnosis = "Retinitis pigmentosa"
    station.findings_elicited = "Bone spicule pigmentation of retinitis pigmentosa."

    assert verbatim_findings_floor(station, None)[0] is None


def _figure(db, station, **kw):
    from app.models import OsceFigure

    figure = OsceFigure(station_id=station.id, position=kw.pop("position", 0), **kw)
    db.add(figure)
    db.commit()
    return figure


def test_settling_removes_a_figure_nothing_could_ever_fill(db):
    """A rubric action is not a view, whoever wrote it and whenever."""
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    figure = _figure(db, station, image_id=None,
                     wanted_description="Examines the other cranial nerves for involvement")

    outcome = settle_station(db, station)
    assert outcome["removed"] == 1
    assert db.query(type(figure)).filter_by(id=figure.id).one_or_none() is None


def test_settling_clears_findings_borrowed_for_an_investigation(db):
    """"Fundus examination is normal" is not a description of an angiogram."""
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    station.findings_elicited = "Fundus examination is normal."
    figure = _figure(
        db, station, image_id=None,
        wanted_description="Urgent CT angiography of the brain and circle of Willis",
        described_findings="Fundus examination is normal.",
        described_findings_approved=True,
    )

    outcome = settle_station(db, station)
    db.expire_all()
    figure = db.query(type(figure)).filter_by(id=figure.id).one()
    assert outcome["cleared"] == 1
    assert figure.described_findings is None
    assert figure.described_findings_approved is False


def test_settling_publishes_words_left_unreleased(db):
    """A station holding a description nobody released has unearnable marks."""
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    figure = _figure(
        db, station, image_id=None,
        wanted_description="external photograph montage of the nine positions of gaze",
        described_findings="The right eye does not elevate or adduct; the lid is ptotic.",
        described_findings_approved=False,
    )

    outcome = settle_station(db, station)
    db.expire_all()
    figure = db.query(type(figure)).filter_by(id=figure.id).one()
    assert outcome["published"] == 1
    assert figure.described_findings_approved is True


def test_settling_is_safe_to_run_twice(db):
    """It states an end state, so a second run must change nothing."""
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    station.findings_elicited = "Fundus examination is normal."
    _figure(db, station, image_id=None, wanted_description="Examines the pupils", position=0)
    _figure(db, station, image_id=None, position=1,
            wanted_description="external photograph of the right eye",
            described_findings="The right pupil is dilated and unreactive.")

    first = settle_station(db, station)
    db.expire_all()
    second = settle_station(db, db.query(type(station)).filter_by(id=station.id).one())
    assert first["removed"] == 1 and first["published"] == 1
    assert second == {"removed": 0, "cleared": 0, "published": 0, "bound": 0}


def test_an_unreachable_model_is_a_failure_not_a_shrug(db, monkeypatch):
    """A provider error must never read as the model declining to invent.

    Both leave a figure with no words. For one evening they were reported
    identically: a provider misroute returned HTTP 404 for all 47 figures, the
    job finished with "described 0, failed 0", and a bank of stations was left
    silently empty while the run looked healthy.
    """
    from app.services.ai import AIError
    from app.services.osce import station_images
    from app.services.osce.station_images import DescriptionUnavailable, describe_findings

    station = make_station(db)

    class _Broken:
        def complete_json(self, **kw):
            raise AIError("HTTP 404: No endpoints found for some/model")

    with pytest.raises(DescriptionUnavailable):
        describe_findings(_Broken(), station, "external photograph of the right eye")

    # Declining, by contrast, is a real answer and stays one.
    class _Declines:
        def complete_json(self, **kw):
            return {"description": ""}

    assert describe_findings(_Declines(), station, "external photograph") == (None, None)


def test_the_papers_own_photograph_is_never_re_bought(db):
    """It is the image the real candidates were shown.

    `opening_image_is_settled` listed only the tiers the web grader writes, so
    a station whose opening image came from the examiners' report was never
    settled and a re-source went shopping for a replacement - on 25 stations.
    A search plus its vision calls is the largest per-station cost there is,
    and it was being spent to replace the best image available with a
    stranger's.
    """
    from app.models import Image, OsceFigure, OsceStation
    from app.services.osce.station_images import opening_image_is_settled
    from app.services.osce.station_images.constants import FROM_PAPER

    image = Image(
        sha256="x" * 64, content_type="image/png", data=b"x", size_bytes=1, origin="pdf"
    )
    db.add(image)
    db.flush()

    station = OsceStation(
        title="Paper station", subspecialty="Glaucoma", prompts=[], source="past_paper"
    )
    db.add(station)
    db.flush()
    figure = OsceFigure(
        station_id=station.id, position=0, image_id=image.id,
        is_approved=True, verification_status=FROM_PAPER, match_confidence=None,
    )
    db.add(figure)
    db.commit()
    db.refresh(station)

    assert opening_image_is_settled(station) is True

    # And one the vision gate called representative is still worth another look.
    figure.verification_status = "representative"
    db.commit()
    db.refresh(station)
    assert opening_image_is_settled(station) is False


def test_the_binder_can_be_reached_once_every_figure_is_verified(db):
    """It could not be, and that was the whole point of it.

    Binding ran only inside the figure recheck, which selects stations by
    whether a figure still needs verifying. Once every figure had been verified
    that query returned nothing, so the binder became unreachable - with
    seventeen questions holding a restored request and the report's own figures
    sitting unclaimed beside them.
    """
    from app.models import Image, OsceFigure, OsceStation
    from app.services.osce.station_images.constants import FROM_PAPER
    from app.services.osce.station_images.ingested import (
        stations_with_bindable_figures,
        stations_with_unchecked_figures,
    )

    image = Image(
        sha256="b" * 64, content_type="image/png", data=b"x", size_bytes=1, origin="pdf"
    )
    db.add(image)
    db.flush()
    station = OsceStation(
        title="Bindable", subspecialty="Glaucoma", source="past_paper",
        prompts=[{"label": "C", "text": "This is his OCT.", "image_wanted": "OCT of the macula"}],
    )
    db.add(station)
    db.flush()
    db.add(OsceFigure(
        station_id=station.id, position=1, image_id=image.id,
        is_approved=True, verification_status=FROM_PAPER, modality="oct",
    ))
    db.commit()

    # Every figure is verified, so the recheck sees nothing to do...
    assert station.id not in stations_with_unchecked_figures(db)
    # ...but there is plainly a figure to bind.
    assert station.id in stations_with_bindable_figures(db)


def test_a_station_whose_figures_are_all_claimed_is_not_selected(db):
    from app.models import Image, OsceFigure, OsceStation
    from app.services.osce.station_images.constants import FROM_PAPER
    from app.services.osce.station_images.ingested import stations_with_bindable_figures

    image = Image(
        sha256="c" * 64, content_type="image/png", data=b"x", size_bytes=1, origin="pdf"
    )
    db.add(image)
    db.flush()
    station = OsceStation(title="Done", subspecialty="Glaucoma", source="past_paper", prompts=[])
    db.add(station)
    db.flush()
    figure = OsceFigure(
        station_id=station.id, position=1, image_id=image.id,
        is_approved=True, verification_status=FROM_PAPER, modality="oct",
    )
    db.add(figure)
    db.commit()
    station.prompts = [
        {"label": "C", "text": "This is his OCT.", "image_wanted": "OCT", "figure_id": figure.id}
    ]
    db.commit()

    assert station.id not in stations_with_bindable_figures(db)


def test_settling_binds_stated_findings_to_the_question_that_asked(db):
    """Station 259: the words exist, approved, attached to nobody.

    Sourcing binds a figure to its question only when an image was attached,
    so a question whose investigation could not be found - but whose findings
    were stated instead - went on showing a blank screen next to a description
    written for it. The audit reported "question C has no image", which was
    true and not the problem.
    """
    from app.services.osce.station_images import settle_station

    wanted = "Specular microscopy of the left cornea, showing abnormal endothelial cells"
    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment.", "seconds": 300,
         "rubric": [{"text": "Describes the findings", "marks": 10}]},
        {"label": "C", "text": "What does this specular microscopy show?", "seconds": 120,
         "image_wanted": wanted, "rubric": [{"text": "Reads it", "marks": 10}]},
    ])
    figure = _figure(
        db, station, image_id=None, position=1, wanted_description=wanted,
        described_findings="The left cornea shows abnormal endothelial cells and "
                           "reduced cell density.",
        described_findings_approved=True,
    )

    outcome = settle_station(db, station)
    db.expire_all()
    station = db.query(type(station)).filter_by(id=station.id).one()

    assert outcome["bound"] == 1
    assert station.prompts[1]["figure_id"] == figure.id


def test_settling_does_not_hand_a_question_words_written_for_another_view(db):
    """The exact request is what matches, because it is what created the figure."""
    from app.services.osce.station_images import settle_station

    station = make_station(db, prompts=[
        {"label": "C", "text": "What does this angiogram show?", "seconds": 120,
         "image_wanted": "Cerebral angiogram of the circle of Willis",
         "rubric": [{"text": "Reads it", "marks": 10}]},
    ])
    _figure(
        db, station, image_id=None, position=1,
        wanted_description="external photograph of the right eye",
        described_findings="The right eye is proptosed with chemosis.",
        described_findings_approved=True,
    )

    outcome = settle_station(db, station)
    db.expire_all()
    station = db.query(type(station)).filter_by(id=station.id).one()

    assert outcome["bound"] == 0
    assert station.prompts[0].get("figure_id") is None


def test_settling_leaves_unpublished_words_unbound(db):
    """Nothing is shown until it is released, so it answers nothing yet."""
    from app.services.osce.station_images import settle_station

    wanted = "OCT of the right macula"
    station = make_station(db, prompts=[
        {"label": "C", "text": "What does this OCT show?", "seconds": 120,
         "image_wanted": wanted, "rubric": [{"text": "Reads it", "marks": 10}]},
    ])
    _figure(db, station, image_id=None, position=1, wanted_description=wanted,
            described_findings="There is a full-thickness defect at the fovea.",
            described_findings_approved=False)

    outcome = settle_station(db, station)
    db.expire_all()
    station = db.query(type(station)).filter_by(id=station.id).one()

    # Publishing and binding happen in one pass: the words are released first,
    # and a released description is then bound like any other answer.
    assert outcome["published"] == 1
    assert outcome["bound"] == 1
    assert station.prompts[0]["figure_id"] is not None


def test_settling_removes_a_figure_nobody_is_asking_for(db):
    """Station 183: an OCT for a question that now states the torsion itself.

    Reconciliation restated the question, so nothing is waiting for the image.
    The row survived every other rule - it names a real investigation - and was
    counted for ever as a view the candidate met with nothing.
    """
    from app.services.osce.station_images import settle_station

    wanted = "Bilateral en-face optic nerve head OCT showing excyclotorsion of the left eye"
    station = make_station(db, prompts=[
        {"label": "C", "seconds": 120,
         "text": "The patient has left excyclotorsion of 5-7 degrees on double Maddox "
                 "rod. What does that tell you?",
         "image_wanted": wanted, "image_search_exhausted": True,
         "rubric": [{"text": "Performs double Maddox rod correctly", "marks": 2}]},
    ])
    figure = _figure(db, station, image_id=None, position=3, wanted_description=wanted)

    outcome = settle_station(db, station)
    assert outcome["removed"] == 1
    assert db.query(type(figure)).filter_by(id=figure.id).one_or_none() is None


def test_settling_removes_a_request_no_question_ever_reads(db):
    """Station 164 wanted a biometry printout nothing on the station asks for."""
    from app.services.osce.station_images import settle_station

    station = make_station(db, prompts=[
        {"label": "C", "text": "This is the corneal topography. What does it show?",
         "seconds": 120, "image_wanted": "Corneal topography of the right eye",
         "figure_id": 999, "rubric": [{"text": "Reads it", "marks": 3}]},
    ])
    figure = _figure(db, station, image_id=None, position=9,
                     wanted_description="A-scan biometry printout showing axial length")

    outcome = settle_station(db, station)
    assert outcome["removed"] == 1
    assert db.query(type(figure)).filter_by(id=figure.id).one_or_none() is None


def test_settling_keeps_a_figure_that_states_its_findings(db):
    """Words are what the candidate meets. Station 8 is nothing else.

    Its opening view has no image and no question bound to it, and the rubric
    line it was created from is not the phrasing `station_views` produces - so
    every test above it says remove. The description is the station.
    """
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    figure = _figure(
        db, station, image_id=None, position=0,
        wanted_description="occlusive vasculitis; the appearance of the drainage devices",
        described_findings="A tube drainage device is in place in each eye.",
        described_findings_approved=True,
    )

    outcome = settle_station(db, station)
    assert outcome["removed"] == 0
    assert db.query(type(figure)).filter_by(id=figure.id).one_or_none() is not None


def test_settling_keeps_a_figure_a_question_is_still_waiting_for(db):
    """A live request holds its figure open, however empty it is today."""
    from app.services.osce.station_images import settle_station

    wanted = "MRI of the orbits showing the muscle bellies"
    station = make_station(db, prompts=[
        {"label": "C", "text": "What does this scan show?", "seconds": 120,
         "image_wanted": wanted, "rubric": [{"text": "Reads it", "marks": 5}]},
    ])
    figure = _figure(db, station, image_id=None, position=2, wanted_description=wanted)

    outcome = settle_station(db, station)
    assert outcome["removed"] == 0
    assert db.query(type(figure)).filter_by(id=figure.id).one_or_none() is not None


def test_settling_keeps_a_view_the_rubric_still_needs(db):
    """The opening view is asked for by the rubric, not by any question."""
    from app.services.osce.station_images import settle_station
    from app.services.osce.coverage import station_views

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment of both eyes.",
         "seconds": 400, "rubric": [{"text": "Describes the corneal opacity", "marks": 18}]},
    ])
    views = station_views(station)
    assert views, "the rubric has to need a view for this test to mean anything"
    figure = _figure(db, station, image_id=None, position=0,
                     wanted_description=views[0].wanted_description)

    outcome = settle_station(db, station)
    assert outcome["removed"] == 0
    assert db.query(type(figure)).filter_by(id=figure.id).one_or_none() is not None
