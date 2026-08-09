"""What the candidate meets on walking in.

A real station shows the patient and hands over the printouts when they are
asked for: the mock station for Joshua Bullock reads "How would you confirm
the diagnosis? - ask for Pentacam/Anterion. Anterion images supplied in
powerpoint". The map is the reward for asking.

Ours opened station 155 on four corneal topography maps and one slit lamp
photograph, which both gave away the answer and buried the view its eight-mark
rubric was written for. 302 investigations across 96 stations were on screen
from the start - 44% of everything a candidate met on walking in.
"""

from __future__ import annotations

from app.api.osce.helpers import opening_figures_payload
from app.models import Image, OsceFigure
from tests.test_api_osce import make_station


def _image(db, tag: str) -> Image:
    image = Image(sha256=tag * 64, content_type="image/jpeg", data=b"jpeg",
                  size_bytes=4, origin="pdf")
    db.add(image)
    db.flush()
    return image


def _figure(db, station, tag=None, modality=None, **kw):
    figure = OsceFigure(
        station_id=station.id,
        position=kw.pop("position", len(station.figures)),
        image_id=_image(db, tag).id if tag else None,
        is_approved=kw.pop("is_approved", True),
        modality=modality,
        **kw,
    )
    db.add(figure)
    db.flush()
    return figure


def _captions(station):
    return [p.get("caption") for p in opening_figures_payload(station)]


def test_an_investigation_does_not_open_the_station(db):
    """Station 155: four topography maps before a word had been said."""
    station = make_station(db)
    _figure(db, station, "a", "slit_lamp", caption="Slit lamp photograph", position=0)
    _figure(db, station, "b", "topography", caption="Corneal topography", position=1)
    _figure(db, station, "c", "oct", caption="OCT of the macula", position=2)
    db.commit()
    db.refresh(station)

    assert _captions(station) == ["Slit lamp photograph"]


def test_the_patient_still_opens_the_station(db):
    """External, slit lamp and fundus are what looking at the patient shows."""
    station = make_station(db)
    for index, (tag, modality) in enumerate(
        [("d", "external"), ("e", "slit_lamp"), ("f", "fundus")]
    ):
        _figure(db, station, tag, modality, caption=modality, position=index)
    db.commit()
    db.refresh(station)

    assert _captions(station) == ["external", "slit_lamp", "fundus"]


def test_an_unclassified_image_is_left_where_it_is(db):
    """"other" means the vision model could not name it, not that it is a printout."""
    station = make_station(db)
    _figure(db, station, "g", "other", caption="A hand-drawn diagram", position=0)
    db.commit()
    db.refresh(station)

    assert _captions(station) == ["A hand-drawn diagram"]


def test_a_station_of_nothing_but_printouts_keeps_them(db):
    """Blank is worse than early.

    A station whose every image is an investigation has no view of the patient
    to fall back on. Withholding them all would leave the candidate looking at
    an empty screen, which is the one outcome they cannot work with.
    """
    station = make_station(db)
    _figure(db, station, "h", "oct", caption="OCT of the macula", position=0)
    _figure(db, station, "i", "visual_field", caption="Humphrey 24-2", position=1)
    db.commit()
    db.refresh(station)

    assert _captions(station) == ["OCT of the macula", "Humphrey 24-2"]


def test_words_are_the_examiner_speaking_and_always_open(db):
    """A stated finding is not a printout, whatever the station holds."""
    station = make_station(db)
    _figure(db, station, "j", "oct", caption="OCT of the macula", position=0)
    _figure(db, station, None, None, position=1,
            described_findings="The right eye turns inwards.",
            described_findings_approved=True)
    db.commit()
    db.refresh(station)

    shown = opening_figures_payload(station)
    assert len(shown) == 1
    assert shown[0]["described_findings"] == "The right eye turns inwards."


def test_a_question_that_discusses_the_scan_is_given_the_scan(db):
    """Ingest lifted these from the report and left them all on the front page.

    The question that asks about the OCT is where the OCT belongs - and it
    never recorded a request for one, because nobody had to ask: it was already
    on screen.
    """
    from app.services.osce.station_images import settle_station

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the fundus of both eyes.", "seconds": 300,
         "rubric": [{"text": "Describes the disc", "marks": 10}]},
        {"label": "B", "text": "Talk me through the OCT of the right macula.",
         "seconds": 240, "rubric": [{"text": "Reads the OCT", "marks": 10}]},
    ])
    _figure(db, station, "k", "fundus", caption="Fundus photograph", position=0)
    scan = _figure(db, station, "l", "oct", caption="OCT of one macula", position=1)
    db.commit()
    db.refresh(station)

    outcome = settle_station(db, station)
    db.expire_all()
    station = db.query(type(station)).filter_by(id=station.id).one()

    assert outcome["bound"] == 1
    assert station.prompts[1]["figure_id"] == scan.id
    assert _captions(station) == ["Fundus photograph"], "and it has left the front page"


def test_a_photograph_of_the_patient_is_never_moved_onto_a_question(db):
    """It is what the station opens on; binding it away empties that screen."""
    from app.services.osce.station_images import settle_station

    station = make_station(db, prompts=[
        {"label": "A", "text": "Describe the slit lamp appearance of the cornea.",
         "seconds": 300, "rubric": [{"text": "Describes it", "marks": 20}]},
    ])
    _figure(db, station, "m", "slit_lamp", caption="Slit lamp photograph", position=0)
    db.commit()
    db.refresh(station)

    settle_station(db, station)
    db.expire_all()
    station = db.query(type(station)).filter_by(id=station.id).one()

    assert station.prompts[0].get("figure_id") is None
    assert _captions(station) == ["Slit lamp photograph"]


def test_a_caption_does_not_outlive_the_photograph_it_described(db):
    """Figure 35 read "Fundus photograph of one eye" for an image long gone.

    Captions are left behind when an image is rejected or detached, and the
    re-captioning pass cannot reach them: it looks at images, and there is
    nothing here to look at.
    """
    from app.services.osce.station_images import settle_station

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the fundus of both eyes.", "seconds": 400,
         "rubric": [{"text": "Describes the disc", "marks": 20}]},
    ])
    figure = _figure(db, station, None, None, position=0,
                     wanted_description="left dragged macula - left eye",
                     caption="Fundus photograph of one eye",
                     described_findings="The left macula is dragged temporally.",
                     described_findings_approved=True)
    db.commit()
    db.refresh(station)

    settle_station(db, station)
    db.expire_all()
    figure = db.query(type(figure)).filter_by(id=figure.id).one()

    assert figure.caption is None
    assert figure.described_findings, "the words are what it has; they stay"


def test_a_stock_photograph_beside_the_patients_own_is_removed(db):
    """Station 210 opened on a stock anterior segment montage searched for iris
    neovascularisation, in front of five slit lamp photographs the examiners
    themselves printed.

    Station 155 is what that costs: the candidate described a graft in the
    wrong eye, off an image that was never this patient.
    """
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    paper = _figure(db, station, "n", "slit_lamp", position=1,
                    verification_status="from_paper", caption="Slit lamp photograph")
    stock = _figure(db, station, "o", "slit_lamp", position=0,
                    verification_status="representative", caption="A slit lamp photograph")
    db.commit()
    db.refresh(station)

    outcome = settle_station(db, station)

    assert outcome["removed"] == 1
    assert db.query(type(stock)).filter_by(id=stock.id).one_or_none() is None
    assert db.query(type(paper)).filter_by(id=paper.id).one_or_none() is not None


def test_a_stock_photograph_of_a_view_the_paper_lacks_is_kept(db):
    """149 of them answer views the report has no picture of at all.

    Removing those would take the station's only image of that view with them,
    which is the opposite of the repair.
    """
    from app.services.osce.station_images import settle_station

    station = make_station(db)
    _figure(db, station, "p", "slit_lamp", position=0,
            verification_status="from_paper", caption="Slit lamp photograph")
    stock = _figure(db, station, "q", "fundus", position=1,
                    verification_status="representative", caption="Fundus photograph")
    db.commit()
    db.refresh(station)

    settle_station(db, station)

    assert db.query(type(stock)).filter_by(id=stock.id).one_or_none() is not None


def test_sourcing_does_not_buy_what_the_paper_already_holds(db, ai):
    """Station 210 bought a stock anterior segment montage while holding five
    slit lamp photographs the examiners printed.

    The settled check reads only the lowest-positioned figure, so a request
    created beneath the paper's own pictures looked unanswered. There is
    nothing a search can find that beats the picture the real candidates were
    shown - and the search costs quota to lose.
    """
    from app.models import Setting
    from app.services.ai import AIClient
    from app.services.osce.station_images import source_image_for_station

    db.add(Setting(key="imagesearch.provider", value="brave", is_encrypted=False))
    db.add(Setting(key="imagesearch.api_key", value="test-key", is_encrypted=False))
    db.commit()

    station = make_station(db, prompts=[
        {"label": "A", "text": "Please examine the anterior segment of both eyes.",
         "seconds": 400, "rubric": [{"text": "Describes the findings", "marks": 20}]},
    ])
    _figure(db, station, "r", "slit_lamp", position=1,
            verification_status="from_paper", caption="Slit lamp photograph")
    db.commit()
    db.refresh(station)

    outcome = source_image_for_station(db, AIClient(db), station)

    assert outcome["attached"] is False
    assert "paper already holds" in outcome["reason"]
    assert outcome["queries"] == [], "no query was written, so none was paid for"
