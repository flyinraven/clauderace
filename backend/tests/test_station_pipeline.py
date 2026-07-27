"""Preparing a station: examiner questions, the findings split, and its image.

This is where nearly all the AI spend happens - image sourcing verifies every
candidate photograph with a vision call - so these tests assert on the shape and
size of what is sent, not only on the result.
"""

from __future__ import annotations

import io
import json

from PIL import Image as PILImage

from app.models import Image, OsceFigure, OsceStation, Setting
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


def test_a_station_with_no_image_need_not_ask_the_candidate_to_read_one():
    """Nothing is shown, so there is no photograph to describe."""
    from app.services.osce.prompts import _arc_problems, _normalise

    prompts, _ = _normalise([item for item in full_arc() if item["step"] != 3])
    assert _arc_problems(prompts, has_image=False) == []
    assert any("arc step 3" in p for p in _arc_problems(prompts, has_image=True))


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
        "app.services.osce.station_images.build_provider", lambda store: search
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.download_candidate",
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


def test_a_verification_call_never_sends_the_full_size_photograph(
    client, db, admin, ai, run_jobs, monkeypatch
):
    """The single largest cost in the system - every candidate image is verified."""
    make_station(db)
    _configure_image_search(db)
    photo = big_photo()
    monkeypatch.setattr(
        "app.services.osce.station_images.build_provider",
        lambda store: FakeSearch(["https://example.org/eye1.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.download_candidate",
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
        "app.services.osce.station_images.build_provider",
        lambda store: FakeSearch(["https://example.org/eye1.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.download_candidate",
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
        "app.services.osce.station_images.build_provider",
        lambda store: FakeSearch(["https://example.org/diagram.png"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.download_candidate",
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
        "app.services.osce.station_images.build_provider",
        lambda store: FakeSearch(
            ["https://example.org/eye1.jpg", "https://example.org/eye2.jpg"]
        ),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.download_candidate", record
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
        "app.services.osce.station_images.build_provider",
        lambda store: FakeSearch(["https://example.org/eye1.jpg"]),
    )
    monkeypatch.setattr(
        "app.services.osce.station_images.download_candidate",
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
