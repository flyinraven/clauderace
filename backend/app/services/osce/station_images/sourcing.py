"""Finding and attaching a station's images.

Three kinds, in the order they are paid for: the view the candidate opens on,
the coverage views a two-eye rubric needs, and an image for any question asking
about something the opening view cannot show.
"""

from __future__ import annotations

import hashlib
import logging

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from app.models import Image, OsceFigure, OsceStation
from app.services.ai import AIClient
from app.services.coerce import as_float
from app.services.errors import log_error
from app.services.imagesearch.base import ImageQueryError, ImageSearchError
from app.services.imagesearch.relevance import (
    modality_mismatch,
    split_investigations,
    unsourceable_reason,
)
from app.services.imagesearch.service import (
    attach_image_to_figure,
    build_provider,
    check_and_consume_quota,
    download_candidate,
)
from app.services.osce.coverage import station_views
from app.services.osce.sittability import answers_a_view
from app.services.settings_store import SettingsStore
from app.services.osce.station_images.constants import (
    MIN_MATCH_CONFIDENCE,
    FROM_PAPER,
    MIN_REPRESENTATIVE_CONFIDENCE,
    SETTLED_MATCH_CONFIDENCE,
)
from app.services.osce.station_images.queries import (
    build_search_queries,
    wants_gaze_montage,
)
from app.services.osce.station_images.verify import (
    expected_modalities_for,
    verbatim_findings_floor,
    blind_disagreement,
    describe_blind,
    label_side,
    side_from_request,
    verify_image,
)
from app.services.osce.station_images.describe import (
    DescriptionUnavailable,
    describe_findings,
)
from app.services.osce.station_images.ingested import bound_figure_ids
from app.services.osce.prompts import PRESENTS_INVESTIGATION_RE

logger = logging.getLogger(__name__)


def source_image_for_station(
    db: Session,
    client: AIClient,
    station: OsceStation,
    job_id: int | None = None,
    figure: OsceFigure | None = None,
) -> dict[str, Any]:
    """Find, verify and attach one image. Returns an outcome summary.

    Pass `figure` to fill a particular one - an ancillary test a question asks
    the candidate to read. Its `wanted_description` then drives both the search
    and the verification. Left out, this fills the station's own image.
    """
    store = SettingsStore(db)
    provider = build_provider(store)
    if provider is None:
        raise ImageSearchError("Image search is disabled (provider is 'none').")

    wanted: str | None = None
    if figure is not None:
        wanted = figure.wanted_description
    else:
        # The rubric decides what the opening image has to show, not the case
        # findings. Searching the findings produced a good picture of the
        # disease and a station whose marks could not be earned from it; the
        # first view states the signs the candidate is actually marked on.
        views = station_views(station)
        opening = views[0].wanted_description if views else (
            station.findings_elicited or station.findings
        )

        # The station's own figure, never a question's. Taking the lowest
        # position outright overwrote question C's MRI on station 158 with the
        # gaze montage this run had just found: the montage ended up attached
        # to "what does this scan show?", and the motility task it was searched
        # for still opened on nothing. A figure a question owns is that
        # question's, and is filled by its own request.
        figure = next((f for f in opening_figures(station)), None)
        if figure is None:
            figure = OsceFigure(
                station_id=station.id,
                position=max((f.position for f in station.figures), default=-1) + 1,
                wanted_description=opening,
            )
            db.add(figure)
            db.flush()
        elif views and figure.wanted_description != opening:
            # Re-sourcing an existing figure has to re-read the rubric too.
            # Setting this only on creation meant every station sourced before
            # the change kept searching its case findings for ever - including
            # the one this was first tested on.
            figure.wanted_description = opening
            db.flush()

    # Whatever this run finds replaces what the last one said, so the previous
    # attempt's wording must not survive it - and approval must not survive the
    # wording. Leaving it in place kept two wrong descriptions on the station
    # through a re-run that was meant to replace them.
    figure.described_findings = None
    figure.described_findings_approved = False

    expected = expected_modalities_for(station, wanted)

    # The examiners' own photograph of this view, already on the station. The
    # settled check only ever looked at the lowest-positioned figure, so a
    # request created beneath five of the paper's slit lamp photographs was
    # answered by buying a stranger's - station 210 opens on a stock anterior
    # segment montage in front of the real patient's, and 44 figures across 28
    # stations are that same purchase. There is nothing a search can find that
    # beats the picture the real candidates were shown.
    claimed = {i for p in (station.prompts or []) for i in bound_figure_ids(p)}
    # Including the figure this call is about to fill. Excluding it meant the
    # guard only caught a stock image sitting BESIDE the paper's own; when the
    # paper's photograph was itself the lowest-positioned figure it was chosen
    # as the one to fill and searched over - buying a stranger's picture to
    # replace the examiners' own, which is the exact thing this prevents
    # everywhere else.
    held = next(
        (
            f
            for f in station.figures
            if f.image_id
            and f.id not in claimed
            and f.verification_status == FROM_PAPER
            and (not expected or f.modality in expected)
        ),
        None,
    )
    if held is not None:
        logger.info(
            "Station %s already holds the examiners' own %s for this view; not searching",
            station.id, held.modality or "image",
        )
        db.commit()
        return {"attached": False, "queries": [], "reason": "the paper already holds this view"}

    queries = build_search_queries(db, client, station, wanted)
    per_query = store.get_int("imagesearch.results_per_query", 6)
    rejections: list[str] = []
    # confidence, candidate, downloaded, verdict, and the query that found it
    best_representative: tuple[float, Any, Any, dict[str, Any], str] | None = None

    # Never offer back an image the user has already turned down.
    already_rejected = set(figure.rejected_urls or [])
    # Nor one this station is already showing, under any figure.
    held_digests = {
        image.sha256
        for image in (
            db.get(Image, f.image_id)
            for f in station.figures
            if f.image_id and f.id != figure.id
        )
        if image is not None
    }
    auto_approve = store.get_bool("imagesearch.auto_approve", True)

    # Two sweeps at most. The first works specific -> broad and stops at the
    # first faithful match, holding any representative hit in reserve rather
    # than accepting it, in case a later query turns up something faithful.
    #
    # A sweep that ends holding only a representative has learned something it
    # did not know when it wrote its queries: the model has just listed the
    # signs that image fails to show. Searching again on those terms is the one
    # cheap chance of an exact image, so it is taken - once. If the second sweep
    # also finds nothing faithful, the representative is accepted rather than
    # thrown away: a real photograph of the pathology beats no picture at all,
    # and the signs it misses are stated beside it.
    held: tuple[float, Any, Any, dict[str, Any], str] | None = None
    for sweep in (1, 2):
        best_representative = None
        for query in queries:
            figure.search_query = query
            try:
                check_and_consume_quota(db, store)
                candidates = provider.search(query, per_query)
            except ImageQueryError as exc:
                # This phrase, not this account. Brave answered HTTP 500 to
                # "nine positions of gaze photograph Congenital fibrosis of
                # extraocular muscles." and both batches then running were
                # failed outright, leaving 35 stations unsourced over one bad
                # query.
                logger.info("Search failed for %r: %s", query, exc)
                rejections.append(f"search failed for '{query}': {exc}")
                continue
            except ImageSearchError:
                raise
            if not candidates:
                rejections.append(f"no results for '{query}'")
                continue

            for candidate in candidates:
                if candidate.image_url in already_rejected:
                    continue
                downloaded = download_candidate(candidate)
                if downloaded is None:
                    # Silently skipping these left "tried 3 queries" and no
                    # reason at all in the notes, when in fact every result was
                    # a thumbnail too small to describe.
                    rejections.append(
                        f"{candidate.source or 'source'}: not usable (too small, wrong "
                        "format, or would not download)"
                    )
                    continue

                # A picture this station already shows. Searching a second view
                # of the same case finds the same photograph, and it was
                # attached again under a new caption: station 28 held one gaze
                # montage as both figure 0 and figure 2, so the candidate met
                # the same image twice and the second view was never covered.
                # Rejecting it here also stops the vision call being paid for.
                if hashlib.sha256(downloaded[0]).hexdigest() in held_digests:
                    rejections.append(
                        f"{candidate.source or 'source'}: the station already shows this image"
                    )
                    continue
                blob, content_type, _width, _height = downloaded

                try:
                    verdict = verify_image(db, client, station, blob, content_type, wanted)
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    logger.debug("Verification error on a candidate: %s", exc)
                    continue

                tier = str(verdict.get("tier") or "reject").lower()
                confidence = as_float(verdict.get("confidence"), 0.0)

                # The grader judges pathology; this judges whether the image is
                # the examination that was asked for. An angiogram passed on the
                # station's disc findings is still the wrong thing to hand
                # someone told to examine the anterior segment.
                mismatch = modality_mismatch(expected, verdict.get("modality"))
                if mismatch:
                    rejections.append(f"{candidate.source or 'source'}: {mismatch}")
                    continue

                if tier == "faithful" and confidence >= MIN_MATCH_CONFIDENCE:
                    return _attach(db, client, station, figure, candidate, downloaded, verdict,
                                   "faithful", confidence, query, len(rejections), auto_approve)

                if tier == "representative" and confidence >= MIN_REPRESENTATIVE_CONFIDENCE:
                    if best_representative is None or confidence > best_representative[0]:
                        best_representative = (confidence, candidate, downloaded, verdict, query)
                    continue

                rejections.append(
                    f"{candidate.source or 'source'}: {verdict.get('reason') or 'rejected'}"
                )

        # Keep whichever sweep found the better representative.
        if best_representative is not None and (held is None or best_representative[0] > held[0]):
            held = best_representative

        if sweep == 2 or held is None:
            break

        missing = str(held[3].get("missing") or "").strip()
        if not missing or missing.lower() in {"null", "none"}:
            # It misses nothing anyone can name, so there is nothing to search
            # on that the first sweep did not already try.
            break
        # The reserve is not offered back to the second sweep - it is already
        # held, and re-verifying it would spend a vision call to learn what is
        # already known.
        already_rejected.add(held[1].image_url)
        queries = build_search_queries(
            db, client, station, f"{wanted or ''} showing {missing}".strip()
        )
        logger.info(
            "Station %s figure %s: re-sourcing once on the signs the best image misses (%r)",
            station.id, figure.id, missing[:120],
        )

    if held is not None:
        confidence, candidate, downloaded, verdict, found_by = held
        # The query that found it, not whichever was tried last. Station 7's
        # montage was recorded against "multiple cranial nerve palsies", the
        # broad phrase that had already failed, which reads as though the gaze
        # wording had made no difference when it was the whole reason.
        return _attach(db, client, station, figure, candidate, downloaded, verdict,
                       "representative", confidence, found_by,
                       len(rejections), auto_approve)

    # A search that found nothing must not cost the station the image it
    # already had. This cleared it first and searched second, so every empty
    # re-source stripped a picture: station 119 went into a run holding an
    # approved nine-position montage and came out with an empty figure still
    # marked approved, showing the candidate nothing.
    #
    # Only a figure that arrived here with nothing is left as rejected. One
    # that already had an image keeps it, and the notes say the replacement
    # search failed.
    tried = (
        f"Tried {len(queries)} quer(ies): {'; '.join(queries)}. "
        + " | ".join(rejections[:5])
    )
    if figure.image_id is not None:
        figure.verification_notes = (
            f"{figure.verification_notes or ''}  [Kept the previous image: a "
            f"replacement search found nothing. {tried}]"
        )[:4000]
        db.commit()
        return {"attached": False, "queries": queries, "reason": "kept the existing image",
                "rejected": len(rejections)}

    figure.verification_status = "rejected"
    figure.verification_notes = tried[:4000]

    # Last resort, and only here: every query has been tried and not one
    # candidate survived, not even a representative. Rather than leave a
    # station whose marks cannot be earned, the examiner states the findings
    # the way a real patient would demonstrate them.
    try:
        described, concern = describe_findings(
            client, station, wanted or figure.wanted_description
        )
    except DescriptionUnavailable as exc:
        # The search already failed; the words could not even be attempted. Say
        # so on the figure rather than leaving a station that reads as though
        # nothing could be said about it.
        described, concern = None, None
        figure.verification_notes = (
            f"{figure.verification_notes or ''}  [Could not write the findings: {exc}]"
        ).strip()[:4000]
    if not described:
        # The model is told to return nothing rather than invent, and on a
        # station whose findings are terse it does exactly that - leaving four
        # stations with no image and no words either, which is the one outcome
        # a candidate cannot work with.
        #
        # The station's own recorded findings are not an invention: they are
        # what the examiners printed. Stated verbatim they are always available
        # and always true, so they are the floor beneath the model rather than
        # a rival to it. The leak guard still applies - findings that name the
        # diagnosis cannot be read out - and it is held for review like any
        # other wording.
        described, concern = verbatim_findings_floor(
            station, wanted or figure.wanted_description
        )
        if described:
            logger.info("Station %s falls back to its recorded findings", station.id)

    if described:
        figure.described_findings = described
        figure.verification_status = "described"
        # Shown, not held. The station has no image and every search for one
        # has failed, so words are all there is: holding them for approval
        # leaves the marks unearnable and waits on somebody noticing. The leak
        # guard above is the check that matters and it has already run.
        figure.described_findings_approved = True
        if concern:
            figure.verification_notes = (
                f"{figure.verification_notes or ''}  "
                f"[Check the stated findings: {concern}]"
            )[:4000]
    db.commit()
    return {"attached": False, "queries": queries, "reason": "all candidates rejected",
            "rejected": len(rejections)}


def _attach(
    db: Session,
    client: AIClient,
    station: OsceStation,
    figure: OsceFigure,
    candidate: Any,
    downloaded: Any,
    verdict: dict[str, Any],
    tier: str,
    confidence: float,
    query: str,
    rejected: int,
    auto_approve: bool = True,
) -> dict[str, Any]:
    # OsceFigure exposes the same `image_id` the writer sets, so the
    # deduplicate-and-store path is reused as-is.
    image = attach_image_to_figure(db, figure, candidate, downloaded)
    figure.image_id = image.id

    # A second look, with the station withheld. The graded verdict above was
    # given the expected signs and can agree with them without having seen
    # them; this one has nothing to agree with. Its caption is the one stored,
    # because questions are now matched to their images by what the caption
    # says - an echoing caption hides the mismatch that check exists to find.
    blind: dict[str, Any] = {}
    try:
        blind = describe_blind(client, downloaded[0], downloaded[1])
    except Exception as exc:  # noqa: BLE001 - a caption is not worth losing the image over
        logger.warning("Blind description failed for figure %s: %s", figure.id, exc)

    side = str(blind.get("side") or "").strip().lower()
    if side not in {"right", "left", "both"}:
        # What the image shows wins; the view it was searched for fills the gap.
        side = side_from_request(figure.wanted_description)
    figure.caption = label_side(
        str(blind.get("caption") or "").strip()
        or str(verdict.get("caption") or "").strip(),
        side,
    )
    figure.modality = str(blind.get("modality") or verdict.get("modality") or "").strip() or None
    figure.verification_status = tier
    notes = str(verdict.get("shows") or "").strip()

    disagreement = blind_disagreement(blind, figure.wanted_description, station)
    if disagreement:
        # Downgraded, never rejected. The image is real and a station with a
        # picture and an honest note beside it beats a station with a gap.
        if tier == "faithful":
            tier = "representative"
            confidence = min(confidence, MIN_REPRESENTATIVE_CONFIDENCE)
        notes = f"{notes}  [Looked at without the station: {disagreement}]"
    missing = str(verdict.get("missing") or "").strip()
    if tier == "representative" and missing and missing.lower() not in {"null", "none"}:
        notes = f"{notes}  [Does NOT show: {missing}]"
        # The image is a genuine picture of the pathology but cannot answer
        # every expectation, and those are marks the candidate is about to be
        # asked for. State the ones it misses, the way the patient would have
        # demonstrated them, so the rubric stays earnable alongside the image.
        described, concern = describe_findings(client, station, missing)
        figure.described_findings = described
        if described and concern:
            notes = f"{notes}  [Check the stated findings: {concern}]"
    else:
        figure.described_findings = None
    figure.verification_notes = notes or None
    figure.match_confidence = confidence
    figure.search_query = query
    # Vision verification has already discarded diagrams, wrong modalities and
    # unrelated pathology, so a faithful image is shown straight away and the
    # user rejects the ones that are wrong. Holding everything back for
    # approval means stations start with no image at all, which is worse.
    #
    # A representative one reaches here only after a second sweep searched
    # specifically for the signs it misses and still found nothing better, so
    # the choice it represents is not "this or an exact image" but "this or
    # nothing". It is accepted on the same terms, with the missing signs stated
    # beside it. Holding it for a human instead was what filled the review queue
    # with hundreds of stations that had no decision left in them.
    figure.is_approved = auto_approve
    db.commit()
    return {
        "attached": True, "tier": tier, "query": query,
        "confidence": confidence, "source_url": candidate.image_url,
        "rejected": rejected,
    }


def source_coverage_images(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Fill every view the station's rubric needs, not just the opening one.

    A task marking signs in both eyes cannot be answered from one photograph:
    the marks for the other eye are unearnable however good the candidate is.
    Each view the rubric implies gets its own figure, so the whole rubric is
    describable.
    """
    views = station_views(station)
    if len(views) <= 1:
        return {"attached": 0, "failed": 0, "views": len(views)}

    # The opening figure already covers the first view; it was sourced from the
    # first task and is what the candidate sees on entering.
    #
    # Indexed by what each figure wants, not just the ones that found an image.
    # Keying on `image_id is not None` meant a view that had come back with
    # nothing - or with a written description instead - was treated as absent,
    # so every re-run added another figure for it and left the old one behind
    # with its stale text still attached to the station.
    by_wanted: dict[str, OsceFigure] = {}
    for existing in station.figures:
        key = (existing.wanted_description or "").strip().lower()
        if key and key not in by_wanted:
            by_wanted[key] = existing
    attached, failed = 0, 0

    for view in views[1:]:
        wanted = view.wanted_description
        figure = by_wanted.get(wanted.strip().lower())
        if figure is not None and figure.image_id is not None:
            continue  # already answered by a real image
        if figure is None:
            figure = OsceFigure(
                station_id=station.id,
                position=len(station.figures) + 1,
                wanted_description=wanted,
            )
            db.add(figure)
        db.flush()
        try:
            outcome = source_image_for_station(db, client, station, job_id, figure=figure)
        except ImageSearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - one view must not stop the rest
            db.rollback()
            logger.exception("Could not source the %s view for station %s",
                             view.laterality, station.id)
            log_error(db, source="osce_images", message=str(exc),
                      context={"station_id": station.id, "view": view.laterality})
            failed += 1
            continue
        if outcome.get("attached"):
            attached += 1
        else:
            failed += 1

    db.commit()
    return {"attached": attached, "failed": failed, "views": len(views)}


def source_prompt_images(
    db: Session, client: AIClient, station: OsceStation, job_id: int | None = None
) -> dict[str, Any]:
    """Find the ancillary images the station's questions ask the candidate to read.

    A question that says "This is an MRI of the orbits, what does it show?" is
    only askable if there is an MRI. The question states what it needs, this
    goes and gets it, and the figure is bound to that question so it appears
    when the question does - showing it from the start would hand the candidate
    the diagnosis before they have described anything.

    A question may ask for more than one: "OCT of the right macula showing CNVM
    and fluorescein angiogram of both eyes showing multifocal choroiditis" is
    two investigations, and no one image is both. Searching the whole string
    returned nothing at all, so nine such questions kept asking for something
    that was never going to arrive. Each investigation gets its own figure and
    the question carries them all.
    """
    prompts = list(station.prompts or [])
    attached, failed, skipped, described = 0, 0, 0, 0

    for index, prompt in enumerate(prompts):
        wanted = prompt.get("image_wanted")
        if not wanted or prompt.get("figure_id"):
            continue
        # Reconciliation has already restated this question as what the
        # candidate would expect, because no image could be found for it. The
        # request is kept so the binder can still match a figure the paper
        # holds - which costs nothing - but searching again would buy the same
        # failure twice.
        if prompt.get("image_search_exhausted"):
            continue

        # No search will ever fill a serology titre or a textbook diagram. Left
        # in, they were paid for on every run and reported as merely missing.
        impossible = unsourceable_reason(wanted)
        if impossible:
            prompt["image_impossible"] = impossible
            skipped += 1
            continue

        bound: list[int] = []
        for part in split_investigations(wanted):
            figure = OsceFigure(
                station_id=station.id,
                # After every figure the station already has: position 0 is the
                # patient, and ordering keeps the opening image first.
                position=len(station.figures) + len(bound) + 1,
                wanted_description=part,
            )
            db.add(figure)
            db.flush()

            try:
                outcome = source_image_for_station(db, client, station, job_id, figure=figure)
            except ImageSearchError:
                raise
            except Exception as exc:  # noqa: BLE001 - one question must not stop the rest
                db.rollback()
                logger.exception("Could not source the image for prompt %s", prompt.get("label"))
                log_error(db, source="osce_images", message=str(exc),
                          context={"station_id": station.id, "prompt": prompt.get("label")})
                failed += 1
                continue

            if outcome.get("attached"):
                bound.append(figure.id)
                attached += 1
            elif answers_a_view(figure):
                # No image, but the last resort wrote the findings and published
                # them. That is an answer, and binding only what was attached
                # left it on a figure the question never shows - so the words
                # were written, approved, and read by nobody.
                bound.append(figure.id)
                described += 1
            else:
                # Nothing suitable was found. The question would ask the
                # candidate to read a blank screen, so the figure is left for an
                # admin to fill by hand and the request stays on the question.
                failed += 1

        if bound:
            # Bound by id, so the sitting shows these at this question alone.
            # `figure_id` stays the first of them: it is what every reader of a
            # station still expects, and a question with one investigation - the
            # common case - is unchanged by any of this.
            prompt["figure_id"] = bound[0]
            prompt["figure_ids"] = bound

    if attached or skipped or described:
        # Rebinding alone does not survive: the intermediate commit inside
        # image attachment leaves the column looking unchanged, and the
        # figure_id is quietly dropped. Flagging it dirty is what persists it.
        station.prompts = [dict(p) for p in prompts]
        flag_modified(station, "prompts")
    db.commit()
    return {"attached": attached, "failed": failed, "impossible": skipped,
            "described": described}


def opening_figures(station: OsceStation) -> list[OsceFigure]:
    """The figures the candidate meets on entering, in order.

    A figure bound to a question is not one of them: it appears when that
    question does. Counting them together is how a motility station whose
    question C owns an MRI came to look as though it had an opening image, so
    no gaze montage was ever searched for and the candidate was asked to
    examine eye movements with nothing but a brain scan on screen.
    """
    claimed = {i for p in (station.prompts or []) for i in bound_figure_ids(p)}
    return sorted(
        (f for f in station.figures if f.id not in claimed),
        key=lambda f: f.position,
    )


def opening_image_is_settled(station: OsceStation) -> bool:
    """Whether the station's own image is good enough to leave alone.

    Every other image a station needs is missing or it is not, and the cost of
    finding it is unavoidable. The opening one is different: most stations in a
    batch already have a perfectly good photograph and are in the batch only
    because a question further down wants an MRI. Re-sourcing those spends a
    search and a vision call per station to replace a good image with another
    good image - and drops the description an administrator already approved.

    The test is deliberately the one `scripts/audit_station_images.py` applies to
    a figure. The two disagreeing would mean a batch either paid for stations the
    audit calls fine, or skipped ones it will flag again afterwards.
    """
    figure = next((f for f in opening_figures(station) if f.image_id), None)
    if figure is None or figure.image_id is None or not figure.is_approved:
        return False
    if wants_gaze_montage(station, figure):
        return False
    # A representative image is a picture of the right disease and the wrong
    # patient; it is worth another search. `faithful` is what the vision model
    # writes now, `verified` what it wrote before the tiers were named.
    #
    # `from_paper` is settled by definition and was missing from this set. It is
    # the photograph the examiners themselves printed - the real candidates were
    # shown it - so there is nothing a search could find that would be better,
    # and a re-source was buying a stranger's picture over it on 25 stations.
    # Trusting it is also the cheap answer: a search plus its vision calls is
    # the largest per-station cost in the pipeline.
    if figure.verification_status not in {"faithful", "verified", FROM_PAPER}:
        return False
    # No confidence at all is a figure from before the score was recorded, not a
    # bad one - it is left alone, as the audit leaves it.
    return (figure.match_confidence or 1.0) >= SETTLED_MATCH_CONFIDENCE


def _question_is_waiting_for_a_picture(prompt: dict[str, Any]) -> bool:
    """A question that hands a test over and has nothing bound to it.

    Selecting on image_wanted alone missed twelve questions whose request field
    was empty but whose wording - "This is her OCT. What does it show?" - hands
    the test over just as plainly. Those were invisible to sourcing, so nothing
    was ever bought for them and the candidate met a blank screen.
    """
    if bound_figure_ids(prompt) or prompt.get("image_impossible"):
        return False
    if str(prompt.get("image_wanted") or "").strip():
        return True
    return bool(PRESENTS_INVESTIGATION_RE.search(str(prompt.get("text") or "")))


def stations_needing_images(db: Session) -> list[int]:
    """Stations with no verified image yet, or a question still waiting for one."""
    with_image = set(
        db.execute(
            select(OsceFigure.station_id).where(OsceFigure.image_id.is_not(None))
        ).scalars().all()
    )
    all_ids = db.execute(select(OsceStation.id).order_by(OsceStation.id)).scalars().all()
    needed = [i for i in all_ids if i not in with_image]

    # A station can have its opening photograph and still be unusable, because
    # a question asks the candidate to read an MRI that was never sourced, or
    # because its rubric marks both eyes and only one was ever photographed.
    for station in db.execute(select(OsceStation).order_by(OsceStation.id)).scalars():
        if station.id in needed:
            continue
        if any(_question_is_waiting_for_a_picture(p) for p in (station.prompts or [])):
            needed.append(station.id)
            continue
        # Only the views the candidate opens on. A question's own investigation
        # is counted by the check above, and counting it twice made a station
        # whose question owns a scan look covered for its examination task.
        if len([f for f in opening_figures(station) if f.image_id]) < len(
            station_views(station)
        ):
            needed.append(station.id)
    return sorted(needed)
