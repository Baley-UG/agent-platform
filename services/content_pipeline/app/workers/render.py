"""media_render RQ task — compose one render_variant.

Entry point: `app.workers.render.run(variant_id)`.

Gathers the scene videos for the variant's aspect_group + the scenario's
voiceover + music_track, calls the ffmpeg renderer, writes a versioned
`media_assets` row of type `final_video`, links
`render_variant.final_asset_id`, records `generation_calls` for the
compose work (provider='self_ffmpeg', cost_usd=0 — we don't bill ffmpeg),
and rolls up `scenarios.status`.
"""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from sqlmodel import select

from app.core.logging import logger
from app.models.media_assets import MediaAsset
from app.models.music import MusicTrack
from app.models.render_variants import RenderVariant
from app.models.scenarios import Scenario
from app.models.scene_renders import SceneRender
from app.services import generation_calls as calls_svc
from app.services import media_assets as media_svc
from app.services import render_variants as variants_svc
from app.services import renderer as renderer_svc
from app.services.database import session_scope
from app.services.presets import PRESETS


# Presets whose final deliverable is image-shaped (IG feed posts).
# When the scenario's source is photo/carousel AND the target is one of
# these, we skip ffmpeg entirely — the IG publisher will upload the
# slides as a CAROUSEL_ALBUM directly. Reels / Story / TikTok are still
# video targets even with a static source (slideshow path).
_IMAGE_FRIENDLY_PRESETS = {"ig_feed_45", "ig_feed_11"}


def _scene_video_keys_for_variant(session, scenario: Scenario, preset_key: str) -> List[str]:
    """Return the scene video S3 keys for the variant's aspect group, in scene order."""
    preset = PRESETS[preset_key]
    aspect = preset.aspect

    rows = session.exec(
        select(SceneRender)
        .where(SceneRender.scenario_id == scenario.id, SceneRender.aspect_ratio == aspect)
        .order_by(SceneRender.scene_idx)
    ).all()

    keys: List[str] = []
    for r in rows:
        if r.video_asset_id is None:
            raise renderer_svc.FFmpegError(
                f"scene {r.scene_idx} for aspect={aspect} has no video_asset_id; cannot compose"
            )
        asset = session.get(MediaAsset, r.video_asset_id)
        if asset is None:
            raise renderer_svc.FFmpegError(f"video asset {r.video_asset_id} not found")
        keys.append(asset.s3_key)
    if not keys:
        raise renderer_svc.FFmpegError(f"no scene_renders found for aspect={aspect}")
    return keys


def _scene_image_inputs_for_variant(
    session, scenario: Scenario, preset_key: str
) -> tuple[List[str], List[float]]:
    """Return (scene image S3 keys, per-scene durations) for slideshow compose.

    Used when source kind is `photo` or `carousel` — we skip Seedance
    video gen entirely and stitch the fal-generated images directly.
    Durations come from `scenario_json.scenes[i].duration` (fallback 3s).
    """
    preset = PRESETS[preset_key]
    aspect = preset.aspect

    rows = session.exec(
        select(SceneRender)
        .where(SceneRender.scenario_id == scenario.id, SceneRender.aspect_ratio == aspect)
        .order_by(SceneRender.scene_idx)
    ).all()

    keys: List[str] = []
    durations: List[float] = []
    scenes_json = (scenario.scenario_json or {}).get("scenes") or []
    duration_by_idx: dict[int, float] = {}
    for s in scenes_json:
        if isinstance(s, dict):
            try:
                duration_by_idx[int(s.get("idx") or 0)] = float(s.get("duration") or 0)
            except (TypeError, ValueError):
                continue

    for r in rows:
        if r.image_asset_id is None:
            raise renderer_svc.FFmpegError(
                f"scene {r.scene_idx} (aspect={aspect}) has no image_asset_id; cannot compose"
            )
        asset = session.get(MediaAsset, r.image_asset_id)
        if asset is None:
            raise renderer_svc.FFmpegError(f"image asset {r.image_asset_id} not found")
        keys.append(asset.s3_key)
        durations.append(duration_by_idx.get(r.scene_idx, 3.0))
    if not keys:
        raise renderer_svc.FFmpegError(f"no scene_renders found for aspect={aspect}")
    return keys, durations


def _texts_and_transitions(
    scenario: Scenario,
) -> tuple[list["renderer_svc.SceneText"], list[str]]:
    """Pull per-scene on-screen text + transition_out from scenario_json.

    Returns `(scene_texts, scene_transitions)`:
      - `scene_texts` — one SceneText per scene that HAS text, carrying
        both the concatenated-timeline window (video pipeline) and the
        0-based `scene_pos` (slideshow pipeline).
      - `scene_transitions` — transition_out per scene in render order;
        "cut" default. The slideshow xfade chain reads boundary i from
        transitions[i].

    Timeline windows accumulate scene durations in idx order. When a
    scene is missing a duration we assume 3s (matches the slideshow
    fallback) so a single bad scene doesn't shift every later window
    to garbage.
    """
    scenes = (scenario.scenario_json or {}).get("scenes") or []
    ordered = sorted(
        (s for s in scenes if isinstance(s, dict) and s.get("idx") is not None),
        key=lambda s: s.get("idx"),
    )

    texts: list[renderer_svc.SceneText] = []
    transitions: list[str] = []
    elapsed = 0.0
    for pos, scene in enumerate(ordered):
        try:
            duration = float(scene.get("duration") or 0) or 3.0
        except (TypeError, ValueError):
            duration = 3.0
        text = (scene.get("on_screen_text") or "").strip()
        if text:
            texts.append(
                renderer_svc.SceneText(
                    text=text,
                    style=(scene.get("text_style") or "bold_white").strip() or "bold_white",
                    start_sec=elapsed,
                    end_sec=elapsed + duration,
                    scene_pos=pos,
                )
            )
        transitions.append((scene.get("transition_out") or "cut").strip() or "cut")
        elapsed += duration

    return texts, transitions


def _scene_voiceovers(session, scenario: Scenario) -> list["renderer_svc.SceneVoiceover"]:
    """Active per-scene TTS clips, in scene order. Empty list when the
    scenario was voiced via the legacy single-file path."""
    rows = session.exec(
        select(MediaAsset)
        .where(
            MediaAsset.parent_scenario_id == scenario.id,
            MediaAsset.type == "voiceover_scene",
            MediaAsset.replaced_by_id.is_(None),
        )
        .order_by(MediaAsset.parent_scene_idx)
    ).all()
    return [
        renderer_svc.SceneVoiceover(s3_key=r.s3_key, scene_pos=r.parent_scene_idx or 0)
        for r in rows
        if r.s3_key
    ]


def _outro_key(session, scenario: Scenario) -> Optional[str]:
    """Project outro template video, when one exists.

    Convention: `templates.kind == 'outro'` (newest wins). Projects
    without an outro template just skip the append pass.
    """
    from app.models.templates import Template

    row = session.exec(
        select(Template)
        .where(
            Template.project_id == scenario.project_id,
            Template.kind == "outro",
        )
        .order_by(Template.created_at.desc())
    ).first()
    return row.video_s3_key if row and getattr(row, "video_s3_key", None) else None


def _voiceover_key(session, scenario: Scenario) -> Optional[str]:
    if scenario.voiceover_asset_id is None:
        return None
    asset = session.get(MediaAsset, scenario.voiceover_asset_id)
    return asset.s3_key if asset else None


def _music_key(session, scenario: Scenario) -> Optional[str]:
    if scenario.music_track_id is None:
        return None
    track = session.get(MusicTrack, scenario.music_track_id)
    return track.audio_s3_key if track else None


def _publish_as_carousel(*, session, scenario: Scenario, variant: RenderVariant) -> dict:
    """No-ffmpeg path: stitch the existing scene image assets into the
    variant's `final_asset_ids` and ledger a zero-cost compose call.

    Caller already verified `source_kind ∈ {photo, carousel}` and
    `preset_key ∈ _IMAGE_FRIENDLY_PRESETS`. We collect the scene_renders
    for the variant's aspect_ratio (one row per slide), pull their
    `image_asset_id`s in scene order, mark the variant ready as a
    carousel, and return without ever invoking ffmpeg.
    """
    preset = PRESETS[variant.preset_key]
    aspect = preset.aspect

    rows = session.exec(
        select(SceneRender)
        .where(SceneRender.scenario_id == scenario.id, SceneRender.aspect_ratio == aspect)
        .order_by(SceneRender.scene_idx)
    ).all()

    assets: List[MediaAsset] = []
    for r in rows:
        if r.image_asset_id is None:
            variants_svc.mark_failed(
                session,
                variant,
                f"scene {r.scene_idx} (aspect={aspect}) has no image_asset_id",
            )
            variants_svc.recompute_scenario_status_from_variants(session, scenario)
            return {"ok": False, "error": "missing image asset"}
        asset = session.get(MediaAsset, r.image_asset_id)
        if asset is None:
            variants_svc.mark_failed(
                session, variant, f"image asset {r.image_asset_id} not found"
            )
            variants_svc.recompute_scenario_status_from_variants(session, scenario)
            return {"ok": False, "error": "image asset not found"}
        assets.append(asset)

    if not assets:
        variants_svc.mark_failed(
            session, variant, f"no scene_renders for aspect={aspect}"
        )
        variants_svc.recompute_scenario_status_from_variants(session, scenario)
        return {"ok": False, "error": "no scenes"}

    started = time.monotonic()
    variants_svc.mark_composing(session, variant)

    recipe = {
        "preset_key": variant.preset_key,
        "pipeline": "carousel_no_ffmpeg",
        "asset_count": len(assets),
        "asset_ids": [str(a.id) for a in assets],
    }
    variants_svc.mark_ready_carousel(
        session,
        variant,
        assets=assets,
        thumbnail_asset=assets[0],
        render_recipe=recipe,
    )

    latency_ms = int((time.monotonic() - started) * 1000)
    calls_svc.record(
        session,
        project_id=scenario.project_id,
        scenario_id=scenario.id,
        variant_id=variant.id,
        task_key="compose",
        provider="self_ffmpeg",  # ledger taxonomy stays the same so cost-summary lines up
        model_id="carousel_passthrough",
        cost_usd=0.0,
        latency_ms=latency_ms,
        status_="success",
    )

    variants_svc.recompute_scenario_status_from_variants(session, scenario)

    logger.info(
        "carousel_compose_skipped_ffmpeg",
        variant_id=str(variant.id),
        asset_count=len(assets),
    )
    return {
        "ok": True,
        "variant_id": str(variant.id),
        "pipeline": "carousel_no_ffmpeg",
        "asset_ids": [str(a.id) for a in assets],
    }


def run(variant_id: str) -> dict:
    variant_uuid = uuid.UUID(variant_id)

    with session_scope() as session:
        variant = session.get(RenderVariant, variant_uuid)
        if variant is None:
            logger.warning("render_variant_missing", variant_id=variant_id)
            return {"ok": False, "error": "render_variant not found"}

        scenario = session.get(Scenario, variant.scenario_id)
        if scenario is None:
            variants_svc.mark_failed(session, variant, "scenario missing")
            return {"ok": False, "error": "scenario missing"}

        # Gather inputs. Photo/carousel sources use the static images
        # directly (skipping Seedance video gen); reels/videos use the
        # per-scene Seedance mp4 outputs.
        from app.services import scenarios as scenarios_svc

        source_kind = scenarios_svc.scenario_source_kind(session, scenario)

        # Shortcut: when the source is image-only (photo/carousel) AND
        # the target preset publishes natively as an image carousel
        # (ig_feed_*), there's no point running ffmpeg. Wire the
        # per-scene `image_asset_id`s straight into `final_asset_ids`
        # and let the publisher hand them to IG's CAROUSEL_ALBUM
        # endpoint. Saves 4-7 min of zoompan compose per variant.
        if (
            source_kind in ("photo", "carousel")
            and variant.preset_key in _IMAGE_FRIENDLY_PRESETS
        ):
            return _publish_as_carousel(
                session=session,
                scenario=scenario,
                variant=variant,
            )

        scene_video_keys: List[str] = []
        scene_image_keys: List[str] = []
        scene_durations: List[float] = []
        try:
            if source_kind in ("photo", "carousel"):
                scene_image_keys, scene_durations = _scene_image_inputs_for_variant(
                    session, scenario, variant.preset_key
                )
            else:
                scene_video_keys = _scene_video_keys_for_variant(
                    session, scenario, variant.preset_key
                )
        except renderer_svc.FFmpegError as exc:
            variants_svc.mark_failed(session, variant, str(exc))
            variants_svc.recompute_scenario_status_from_variants(session, scenario)
            return {"ok": False, "error": str(exc)}

        scene_texts, scene_transitions = _texts_and_transitions(scenario)

        # Scene-aligned voiceover clips take precedence; the legacy
        # single-file key is only passed when no clips exist (passing
        # both would double the first scene's audio — the scenario's
        # voiceover_asset_id points at clip 0 in per-scene mode).
        scene_voiceovers = _scene_voiceovers(session, scenario)

        # The reel pipeline needs scene durations too now — voice-bus
        # offsets are computed from them (slideshow already had them).
        if not scene_durations:
            scenes_json = (scenario.scenario_json or {}).get("scenes") or []
            ordered = sorted(
                (s for s in scenes_json if isinstance(s, dict) and s.get("idx") is not None),
                key=lambda s: s.get("idx"),
            )
            for s in ordered:
                try:
                    scene_durations.append(float(s.get("duration") or 0) or 3.0)
                except (TypeError, ValueError):
                    scene_durations.append(3.0)

        inputs = renderer_svc.ComposeInputs(
            scene_video_keys=scene_video_keys,
            scene_image_keys=scene_image_keys,
            scene_durations_sec=scene_durations,
            scene_texts=scene_texts,
            scene_transitions=scene_transitions,
            scene_voiceovers=scene_voiceovers,
            voiceover_key=None if scene_voiceovers else _voiceover_key(session, scenario),
            music_key=_music_key(session, scenario),
            outro_video_key=_outro_key(session, scenario),
        )

        variants_svc.mark_composing(session, variant)
        started = time.monotonic()
        try:
            output = renderer_svc.compose_variant(
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                preset_key=variant.preset_key,
                inputs=inputs,
                output_filename=f"scenario-{scenario.id}-{variant.preset_key}-v{variant.id}.mp4",
            )
        except renderer_svc.FFmpegError as exc:
            logger.warning("compose_failed", variant_id=variant_id, error=str(exc))
            variants_svc.mark_failed(session, variant, str(exc))
            variants_svc.recompute_scenario_status_from_variants(session, scenario)
            calls_svc.record(
                session,
                project_id=scenario.project_id,
                scenario_id=scenario.id,
                variant_id=variant.id,
                task_key="compose",
                provider="self_ffmpeg",
                model_id="ffmpeg",
                status_="failed",
                error=str(exc)[:1000],
                cost_usd=0.0,
            )
            return {"ok": False, "error": str(exc)}

        latency_ms = int((time.monotonic() - started) * 1000)

        # Versioned media_assets write.
        prior_id = variant.final_asset_id
        prior: Optional[MediaAsset] = session.get(MediaAsset, prior_id) if prior_id else None
        metadata = {
            "preset_key": variant.preset_key,
            "render_recipe": output["recipe"],
            "ffmpeg_cmd": output["ffmpeg_cmd"],
        }
        if prior is not None:
            new_asset = media_svc.replace(
                session,
                prior,
                s3_key=output["s3_key"],
                mime_type="video/mp4",
                size_bytes=output["file_size_bytes"],
                width=PRESETS[variant.preset_key].width,
                height=PRESETS[variant.preset_key].height,
                metadata=metadata,
            )
        else:
            new_asset = media_svc.create_initial(
                session,
                project_id=scenario.project_id,
                type_="final_video",
                s3_key=output["s3_key"],
                mime_type="video/mp4",
                size_bytes=output["file_size_bytes"],
                width=PRESETS[variant.preset_key].width,
                height=PRESETS[variant.preset_key].height,
                parent_scenario_id=scenario.id,
                metadata=metadata,
            )

        variants_svc.mark_ready(
            session,
            variant,
            final_asset=new_asset,
            file_size_bytes=output["file_size_bytes"],
            render_recipe=output["recipe"],
        )

        calls_svc.record(
            session,
            project_id=scenario.project_id,
            scenario_id=scenario.id,
            variant_id=variant.id,
            task_key="compose",
            provider="self_ffmpeg",
            model_id="ffmpeg",
            cost_usd=0.0,
            latency_ms=latency_ms,
            status_="success",
        )

        variants_svc.recompute_scenario_status_from_variants(session, scenario)

        return {
            "ok": True,
            "variant_id": str(variant.id),
            "asset_id": str(new_asset.id),
            "version": new_asset.version,
            "file_size_bytes": output["file_size_bytes"],
        }
