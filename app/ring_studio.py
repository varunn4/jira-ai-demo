"""Diamond / Gold / Solitaire Ring — image-generation prompt studio.

This module powers the "Ring Studio" admin dashboard tab (capability key
``rings``). It ports the CELESTE-style luxury jewelry spec-sheet prompt kit into
a small, self-contained service:

* value banks for every ``{{slot}}`` in the master prompt,
* a deterministic assembler (``build_prompt``) that mirrors the reference Python
  generator — ``seed`` makes any design reproducible,
* batch generation (N unique prompts) for the 1000-image run, and
* optional image rendering through the OpenAI image model when
  ``OPENAI_API_KEY`` is configured (otherwise the assembled prompt is returned
  for manual use).

Everything here is stateless apart from rendered PNGs, which are written under
``app/static/rings/`` so the SPA can display/download them.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.config import Settings

log = logging.getLogger(__name__)

# ─── Value banks ─────────────────────────────────────────────────────────────
# Randomize across these for variety. Keys match the meta dict returned by
# build_prompt so the UI can render one dropdown per bank.

BANKS: dict[str, list[str]] = {
    "ring_name": [
        "Celeste", "Aurelia", "Seraphine", "Lumière", "Isolde", "Ophelia", "Amara",
        "Vesper", "Elara", "Solene", "Noor", "Liora", "Camélia", "Estelle", "Anaïs",
        "Marisol", "Aveline", "Rosalind", "Thalia", "Belle Étoile",
    ],
    "ring_subtitle": [
        "Solitaire Diamond Ring", "Halo Diamond Ring", "Three-Stone Diamond Ring",
        "Pavé Solitaire Ring", "Vintage-Inspired Diamond Ring", "Cathedral Solitaire Ring",
        "Twisted Band Diamond Ring", "Bezel Solitaire Ring", "Hidden Halo Solitaire",
    ],
    "diamond_shape": [
        "Round Brilliant", "Oval", "Cushion", "Princess", "Emerald", "Pear",
        "Marquise", "Radiant", "Asscher", "Heart",
    ],
    "carat": ["0.50", "0.70", "0.90", "1.00", "1.25", "1.50", "1.75", "2.00", "2.50", "3.00"],
    "color": ["D", "E", "F", "G", "H", "I"],
    "clarity": ["FL", "IF", "VVS1", "VVS2", "VS1", "VS2"],
    "cut": ["Excellent", "Ideal", "Very Good"],
    "metal": [
        "18K White Gold", "18K Yellow Gold", "18K Rose Gold", "Platinum 950",
        "14K White Gold", "14K Yellow Gold", "14K Rose Gold", "Two-Tone White & Rose Gold",
    ],
    "band_width": [
        "1.6 mm (Tapered)", "1.8 mm", "2.0 mm (Tapered)", "2.2 mm", "2.5 mm", "1.5 mm (Knife-Edge)",
    ],
    "setting": [
        "4 Prong Solitaire", "6 Prong Solitaire", "4 Prong Tulip Setting with Hidden Halo",
        "Bezel Setting", "Cathedral Setting", "Halo Setting", "Double Halo", "Pavé Cathedral",
        "Trellis Setting", "Compass-Set (North-South-East-West)",
    ],
    "background": ["pure white"],
    "accent_color": ["soft gold", "rose-gold", "warm taupe", "muted bronze"],
    "ring_size": ["US 5 (15.7 mm)", "US 6 (16.5 mm)", "US 6.5 (16.9 mm)", "US 7 (17.3 mm)"],
    "motif": ["tulip", "rose", "lily", "orchid", "vine", "laurel"],
}

# (total_diamonds, accent_carat) pairs — paired sensibly.
DIAMOND_PAIRS: list[tuple[int, str]] = [
    (14, "0.14"), (17, "0.18"), (22, "0.24"), (30, "0.35"), (44, "0.50"), (1, "0.00"),
]

# (detail_label, detail_feature) pairs for the DETAIL VIEW thumbnail.
DETAILS: list[tuple[str, str]] = [
    ("HIDDEN HALO", "hidden halo of micro-pavé beneath the center stone"),
    ("GALLERY", "open gallery and prong basket"),
    ("BAND", "pavé-set shoulders"),
    ("PROFILE", "knife-edge band meeting the head"),
    ("BASKET", "diamond-encrusted basket"),
]

INSPIRATIONS: list[str] = [
    "Inspired by the night sky, {name} captures the brilliance of a star in its purest form.",
    "Drawn from morning dew on petals, {name} holds light like the first bloom of spring.",
    "Echoing ocean tides, {name} carries a quiet, endless motion.",
    "Inspired by candlelight, {name} glows soft and timeless.",
]

MOOD: list[str] = [
    "a starfield/galaxy", "a dewy flower petal", "an ocean wave", "a candle flame",
    "frost crystals", "a golden sunrise",
]

HIGHLIGHTS: list[str] = [
    "Tulip setting elevates the diamond for maximum brilliance",
    "Hidden halo adds sparkle from every angle",
    "Pavé-set band for a delicate yet radiant look",
    "Balanced, elegant and timeless design",
    "Cathedral shoulders protect the center stone",
    "Knife-edge band for a sleek modern profile",
    "Comfort-fit interior for all-day wear",
    "Hand-set micro-pavé along the shank",
]

DESCRIPTIONS: list[str] = [
    "A timeless solitaire that celebrates the brilliance of a {shape} diamond. The delicate band with hidden details adds elegance from every angle.",
    "An heirloom-worthy design pairing a luminous {shape} centre with a hand-finished band built to last a lifetime.",
    "Understated and radiant, this ring frames a {shape} diamond in {metal} for effortless everyday glamour.",
]

TEMPLATE = """Create a single high-resolution luxury jewelry catalog spec-sheet image in landscape 4:3 for a Ring. Generate ONE physical ring only. Every panel is a different camera angle or crop of this identical ring. Do not redesign, reinterpret or alter any feature between panels. Treat every panel as a render of the same 3D CAD model viewed from different camera positions.

MANDATORY INSTRUCTION (SUPERSEDES ANY CONFLICTING INSTRUCTIONS): Do not place any text, engraving, hallmark, logo or watermark on the jewelry itself. Catalog typography is allowed only in the layout panels.

DESIGN LANGUAGE: Use the design tradition as inspiration — draw on the pan-Indian jewelry heritage. The uploaded reference image is a stylistic reference only. Create a new design by changing at least 30% of the visible design language, including motif geometry, silhouette, stone arrangement, gallery, proportions and metal flow, while preserving the overall aesthetic direction.. Avoid producing a near-copy.

STYLE: clean premium editorial. Clean seamless {background} studio background (bright #FFFFFF — no cream, beige or grey tint), diffused lighting, realistic diamond fire and reflections. Thin {accent_color} hairline panel borders. Elegant serif headings, clean sans-serif body. Photorealistic 3D render, tack-sharp, no clutter.

LAYOUT (composite grid):
- Header top-left: ring name "{ring_name}" in large elegant serif capitals; subtitle "{ring_subtitle}" in spaced small-caps beneath; small diamond glyph accent.
- Description paragraph: "{description}"
- Left spec column with faceted-diamond bullets:
  CENTER STONE — {diamond_shape}, {carat} ct (Approx.), {color} Color / {clarity} Clarity / {cut} Cut
  METAL — {metal}
  BAND WIDTH — {band_width}
  SETTING — {setting}
  TOTAL DIAMONDS — {total_diamonds} (~{accent_carat} ct)
- Four labeled thumbnails in spaced caps:
  TOP VIEW — dramatic 3/4 perspective, ring upright, centre stone catching light.
  SIDE VIEW — pure profile showing setting height, prongs, band taper.
  FRONT VIEW — upright straight-on elevation.
  DETAIL VIEW ({detail_label}) — macro of the {detail_feature} with fine metalwork and micro-pavé.Do not introduce any new geometry or decorative elements.

Render metal as realistic {metal}. Diamonds must look like genuine cut gemstones. Balanced, airy, gallery-grade. No watermark, no hands."""

# The full ordered list of meta keys the template consumes.
META_KEYS = [
    "ring_name", "ring_subtitle", "description", "diamond_shape", "carat", "color",
    "clarity", "cut", "metal", "band_width", "setting", "total_diamonds", "accent_carat",
    "detail_label", "detail_feature", "background", "accent_color", "inspiration",
    "mood_image", "ring_size", "highlight_1", "highlight_2", "highlight_3", "highlight_4",
    "motif",
]


# ─── Prompt assembly ─────────────────────────────────────────────────────────

def list_banks() -> dict[str, Any]:
    """Everything the UI needs to render the design form (dropdown options)."""
    return {
        "banks": BANKS,
        "diamond_pairs": [{"total_diamonds": t, "accent_carat": a} for t, a in DIAMOND_PAIRS],
        "details": [{"detail_label": lbl, "detail_feature": feat} for lbl, feat in DETAILS],
        "inspirations": INSPIRATIONS,
        "mood_image": MOOD,
        "highlights": HIGHLIGHTS,
        "descriptions": DESCRIPTIONS,
    }


def build_prompt(
    seed: Optional[int] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    """Assemble one master prompt.

    ``seed`` makes the random picks reproducible (same seed → same design).
    ``overrides`` pins any meta field(s) to explicit values; everything else is
    randomized. Unknown override keys are ignored.
    """
    rng = random.Random(seed) if seed is not None else random.Random()
    overrides = {k: v for k, v in (overrides or {}).items() if v not in (None, "")}

    def pick(key: str) -> str:
        if key in overrides:
            return str(overrides[key])
        return rng.choice(BANKS[key])

    name = pick("ring_name")

    if "total_diamonds" in overrides or "accent_carat" in overrides:
        total = overrides.get("total_diamonds")
        acc = overrides.get("accent_carat")
        if total is None or acc is None:
            base_total, base_acc = rng.choice(DIAMOND_PAIRS)
            total = base_total if total is None else total
            acc = base_acc if acc is None else acc
    else:
        total, acc = rng.choice(DIAMOND_PAIRS)

    dlabel = overrides.get("detail_label")
    dfeat = overrides.get("detail_feature")
    if dlabel is None or dfeat is None:
        base_label, base_feat = rng.choice(DETAILS)
        dlabel = base_label if dlabel is None else dlabel
        dfeat = base_feat if dfeat is None else dfeat

    shape = pick("diamond_shape")
    metal = pick("metal")

    if all(f"highlight_{i}" in overrides for i in range(1, 5)):
        hi = [str(overrides[f"highlight_{i}"]) for i in range(1, 5)]
    else:
        hi = rng.sample(HIGHLIGHTS, 4)
        for i in range(1, 5):
            if f"highlight_{i}" in overrides:
                hi[i - 1] = str(overrides[f"highlight_{i}"])

    description = overrides.get("description") or rng.choice(DESCRIPTIONS).format(
        shape=shape.lower(), metal=metal.lower()
    )
    inspiration = overrides.get("inspiration") or rng.choice(INSPIRATIONS).format(name=name)

    fields: dict[str, Any] = {
        "ring_name": name,
        "ring_subtitle": pick("ring_subtitle"),
        "description": description,
        "diamond_shape": shape,
        "carat": pick("carat"),
        "color": pick("color"),
        "clarity": pick("clarity"),
        "cut": pick("cut"),
        "metal": metal,
        "band_width": pick("band_width"),
        "setting": pick("setting"),
        "total_diamonds": total,
        "accent_carat": acc,
        "detail_label": dlabel,
        "detail_feature": dfeat,
        "background": pick("background"),
        "accent_color": pick("accent_color"),
        "inspiration": inspiration,
        "mood_image": overrides.get("mood_image") or rng.choice(MOOD),
        "ring_size": pick("ring_size"),
        "highlight_1": hi[0],
        "highlight_2": hi[1],
        "highlight_3": hi[2],
        "highlight_4": hi[3],
        "motif": pick("motif"),
    }
    return TEMPLATE.format(**fields), fields


def generate_batch(count: int, start_seed: int = 0) -> list[dict[str, Any]]:
    """Generate ``count`` reproducible prompts (seed = start_seed + index).

    Returns a list of ``{index, seed, prompt, meta}`` dicts — the same shape as
    the reference ``ring_prompts.jsonl`` rows.
    """
    count = max(1, min(int(count), 1000))
    out: list[dict[str, Any]] = []
    for i in range(count):
        seed = start_seed + i
        prompt, meta = build_prompt(seed=seed)
        out.append({"index": i, "seed": seed, "prompt": prompt, "meta": meta})
    return out


def batch_to_jsonl(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


# ─── Per-view image rendering (optional) ─────────────────────────────────────
# Instead of one composite spec-sheet, we render FOUR separate images — one per
# camera view (hero 3/4, top, side, front) — all depicting the EXACT SAME ring.
# Each view prompt restates the full ring spec so the design stays identical
# across the four renders.

# Image model. gpt-image-2 renders noticeably sharper diamonds, legible engraving
# and cleaner white backgrounds than gpt-image-1, and holds the per-view poses more
# reliably — so it is the default. Override per-call via the ``model`` argument.
_DEFAULT_MODEL = "gpt-image-2"

# Square size — one ring per frame reads best in a square crop.
_IMAGE_SIZE = "1024x1024"

# The model supports low/medium/high (higher = more output tokens = costlier and
# sharper). Default medium. The UI exposes these three.
_QUALITIES = ("low", "medium", "high")
_DEFAULT_QUALITY = "medium"

# Token pricing, USD per 1M tokens (openai.com/api/pricing). Cost is derived from
# the usage the API returns, so it tracks quality/size exactly rather than relying
# on a hard-coded per-image table. NOTE: these are gpt-image-1 rates — the cost
# figure shown for gpt-image-2 is an approximation until v2 rates are confirmed.
_PRICE_PER_MTOK = {"text_input": 5.0, "image_input": 10.0, "image_output": 40.0}


def _usage_cost_usd(usage: Any) -> Optional[float]:
    """USD cost for one gpt-image-1 call from its ``usage`` object, or None if
    the response carried no usage (older SDK / provider variation)."""
    if usage is None:
        return None

    def field(obj: Any, name: str) -> int:
        val = getattr(obj, name, None)
        if val is None and isinstance(obj, dict):
            val = obj.get(name)
        return int(val or 0)

    details = getattr(usage, "input_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("input_tokens_details")
    text_tokens = field(details, "text_tokens")
    image_tokens = field(details, "image_tokens")
    output_tokens = field(usage, "output_tokens")
    cost = (
        text_tokens * _PRICE_PER_MTOK["text_input"]
        + image_tokens * _PRICE_PER_MTOK["image_input"]
        + output_tokens * _PRICE_PER_MTOK["image_output"]
    ) / 1_000_000
    return round(cost, 6)

# (view key, display label, render mode, source view, camera/view instruction).
# {slots} are filled from meta.
#   mode "anchor" -> rendered fresh from text.
#   mode "edit"   -> rendered by editing the ``src`` view's image, so the ring
#                    (metal, stone, setting, proportions) is carried forward
#                    unchanged rather than re-invented → no hallucination.
#
# Views are rendered SEQUENTIALLY in this order, and each ``src`` must appear
# earlier in the list so its image already exists when the edit runs. TOP, SIDE and
# LAYDOWN all source the HERO so they stay one generation from the anchor and keep
# the identical ring.
#
# TOP used to be a text ANCHOR describing a face-forward elevation, on the theory
# that it rendered more reliably from text than as a big camera move off the hero.
# That was wrong twice over: a face-forward shot is a FRONT elevation, not a top
# view (the head ended up staring at the viewer instead of pointing up), and a text
# anchor rebuilds the ring from the spec fields, which in the upload flow ignores
# the reference entirely. It is now an EDIT off the hero, like the other views.
VIEWS: list[tuple[str, str, str, Optional[str], str]] = [
    ("hero", "Hero 3/4 View", "anchor", None,
     "a dramatic three-quarter perspective: the ring stands upright and is turned "
     "roughly 30 degrees, and the camera is raised ABOVE the ring looking gently "
     "DOWNWARD (about 30 degrees) so BOTH the front face of the setting AND one "
     "shoulder of the band are clearly visible, the centre stone catching the light"),
    ("top", "Top View", "edit", "hero",
     "a symmetric ELEVATED TOP view of the ring STANDING UPRIGHT. Three things must all be true "
     "at once: (1) the ring STANDS upright on its band, (2) the band is fully visible as a "
     "COMPLETE ROUND circle — a closed 'O' you can see through, never collapsed into a flat "
     "straight bar or a thin line, and (3) the head/setting sits at the TOP of that circle with "
     "its face aimed UPWARD toward the sky. The camera is in front of the ring and RAISED about "
     "45 degrees, looking DOWN onto it — high enough to see the TOP FACE of the head, but not so "
     "high that the round band flattens away. So the frame shows the full round band below and "
     "the top surface of the head above, both clearly readable, perfectly symmetric and centred. "
     "CRUCIAL: the head points UP at the raised camera — it must NOT stare straight ahead "
     "horizontally at the viewer like a front elevation. Equally, do NOT shoot from directly "
     "overhead at 90 degrees: that squashes the band into a bar and loses the round. This is NOT "
     "a three-quarter turned view, NOT a side profile, and NOT a low laydown shot with the ring "
     "lying flat. Frame the whole ring with comfortable margin on all sides"),
    ("side", "Side Profile", "edit", "hero",
     "a STRICT SIDE ELEVATION. The ring stands upright on the surface and the camera "
     "sits level with it, viewing the band edge-on from the side so the round band "
     "reads as a COMPLETE OPEN CIRCLE — a clean letter-'O' silhouette you can see "
     "straight through. The setting and centre stone rise from the TOP of that circle, "
     "revealing the full setting height, the prongs and the band taper in pure profile. "
     "This is a strict side profile: it must NOT be a top-down or three-quarter view"),
    ("laydown", "Laydown View", "edit", "hero",
     "a relaxed laid-down product shot: the ring rests on its side/back on the surface and is "
     "photographed from a LOW angle close to the tabletop — the camera sits only slightly above "
     "the surface, about 15 to 25 degrees. CRUCIAL: the main part of the ring — the head/setting "
     "with the centre stone — is rotated to face the camera as DIRECTLY as possible, pointing "
     "straight toward the viewer (not turned off to the side), rising toward us from the band. "
     "The round band is clearly visible as a loop resting on the clean pure-white matte surface, "
     "sweeping back from the head; a soft contact shadow and a gentle reflection sit beneath the "
     "ring. Low, intimate, on-the-table framing with the setting facing us — the ring is LYING "
     "FLAT ON THE SURFACE (the exact OPPOSITE of the standing top view), definitely NOT standing "
     "upright, and the camera is LOW near the surface"),
]

# The HERO view is the reference ANCHOR: it is rendered first from text, then its
# image is fed to every EDIT view so they all depict the identical ring.
VIEW_TEMPLATE = """Create a single high-resolution, photorealistic 3D product render of ONE women's engagement ring — the "{ring_name}" ({ring_subtitle}).

DESIGN LANGUAGE: Use the design tradition as inspiration — draw on the pan-Indian jewelry heritage.

THE RING (this exact design will be reused for four more views, so make it distinctive and consistent):
- Centre diamond: {diamond_shape} cut, {carat} ct, {color} colour / {clarity} clarity / {cut} cut.
- Metal: {metal}. Band width: {band_width}. Setting: {setting}.
- Accent stones: {total_diamonds} diamonds (~{accent_carat} ct total), hand-set micro-pavé.

VIEW: {view_instruction}.

STYLE: luxury jewelry catalog. Clean seamless {background} studio background (bright #ffffff — no cream, beige or grey tint), gentle diffused lighting, realistic diamond fire, subtle reflections and caustics. Tack-sharp focus, the single ring centred in frame, generous negative space, no clutter. Render the metal as realistic {metal}; every diamond must look like a genuine cut gemstone. No text, no labels, no watermark, no hands, no other objects."""

# Hero prompt used when UPLOADED photos of one ring are supplied as reference.
# Deliberately field-FREE: no design fields (metal, carat, shape, setting, ring
# name, design tradition…) are injected, because fabricated values fight the
# reference photos. The ring's material and construction come FROM the uploads;
# only the raw design-language text plus the reinterpretation rules steer it.
BASE_HERO_TEMPLATE = """The reference image(s) show ONE ring from up to four different angles — the SAME ring each time. They are a stylistic reference ONLY.

MANDATORY INSTRUCTION (SUPERSEDES ANY CONFLICTING INSTRUCTIONS): Do not place any text, engraving, hallmark, logo or watermark on the jewelry itself.

DESIGN LANGUAGE: Use the design tradition as inspiration — draw on the pan-Indian jewelry heritage. The uploaded reference image is a stylistic reference only. Create a new design by changing at least 30% of the visible design language, including motif geometry, silhouette, stone arrangement, gallery, proportions and metal flow, while preserving the overall aesthetic direction. Avoid producing a near-copy.

THE RING: take the metal, stones, setting, band and overall proportions FROM THE REFERENCE image(s). Do NOT invent a different specification and do NOT substitute a different metal colour, stone shape or setting type — restyle what is there rather than replacing it. This exact new design will be reused for the other views, so keep it distinctive and consistent.
{reference_details}
VIEW: {view_instruction}.

STYLE: luxury jewelry catalog. Clean seamless {background} studio background (bright #FFFFFF — no cream, beige or grey tint), gentle diffused lighting, realistic diamond fire, subtle reflections and caustics. Tack-sharp focus, the single ring centred in frame, generous negative space, no clutter. Every stone must look like a genuine cut gemstone. No text, no labels, no watermark, no hands, no other objects."""

# Prompt for the non-hero views. The hero image is supplied as a reference so the
# model re-renders THE SAME ring rather than inventing a new one.
EDIT_TEMPLATE = """The reference image shows ONE ring. Re-render THAT EXACT SAME ring — keep its identical metal, stones, setting, band, proportions, colour and finish exactly as they appear in the reference. Do NOT redesign the ring or change any detail. Treat it as the same 3D CAD model, only viewed from a new camera position.

MANDATORY INSTRUCTION (SUPERSEDES ANY CONFLICTING INSTRUCTIONS): Do not place any text, engraving, hallmark, logo or watermark on the jewelry itself.

PROPORTIONS ARE FIXED — this is the single most important requirement. Measure the reference and match it exactly:
- The BAND must keep the identical width and thickness it has in the reference. Do NOT thin it into a wire, do NOT thicken it, do NOT taper it differently.
- The HEAD/setting must keep the identical size, height and depth it has in the reference.
- The RATIO between band thickness and head thickness must be identical to the reference, and the band must meet the shoulders of the head exactly as it does there.
- The band's decoration (filigree, pavé, carving, texture) must continue across the whole band exactly as in the reference — never replace a decorated band with a plain or thinner one.
If the reference shows a broad, chunky band, this render must show the SAME broad chunky band. Only the camera moves; the physical ring is unchanged.

Now MOVE THE CAMERA to a completely different angle. NEW CAMERA VIEW: {view_instruction}. Physically reposition the camera as described — the composition and silhouette MUST clearly differ from the reference; do NOT copy the reference pose.

Keep it photorealistic, luxury jewelry catalog quality, on a clean seamless {background} studio background (bright #FFFFFF — no cream, beige or grey tint) with the single ring centred and generous negative space. No text, no watermark, no hands, no other objects."""


# Generated PNGs live in a subdir of the static mount so the URL (/static/rings/
# generated/<file>) keeps working, while docker-compose can bind-mount a host dir
# over just this folder — that is what makes the Gallery survive a rebuild.
#
# These two MUST stay in step: api.py mounts app/static at /static, so the URL is
# the dir's path relative to app/static. Changing one without the other silently
# serves 404s for every rendered image.
_RINGS_SUBDIR = ("rings", "generated")
_RINGS_URL_PREFIX = "/static/" + "/".join(_RINGS_SUBDIR)


def _rings_static_dir() -> Path:
    d = Path(__file__).resolve().parent.joinpath("static", *_RINGS_SUBDIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


class RingDetails(BaseModel):
    """The physical details of the user's REFERENCE ring, typed in alongside the
    uploaded photos. Every field is optional — the prompt block and the weight
    estimate simply omit whatever is missing."""
    ring_size: Optional[str] = None
    metal_weight: Optional[str] = None   # grams
    gross_weight: Optional[str] = None   # grams

    def filled(self) -> dict[str, str]:
        return {k: str(v).strip() for k, v in self.model_dump().items()
                if v is not None and str(v).strip()}


def _reference_details_block(details: Optional[RingDetails]) -> str:
    """The REFERENCE RING DETAILS paragraph, or '' when nothing was supplied.

    These are user-typed facts about the real ring (not randomized design fields),
    so they belong in the reference prompt as scale/mass context."""
    filled = details.filled() if details else {}
    if not filled:
        return ""
    bits = []
    if "ring_size" in filled:
        bits.append(f"ring size {filled['ring_size']}")
    if "metal_weight" in filled:
        bits.append(f"metal weight {filled['metal_weight']} g")
    if "gross_weight" in filled:
        bits.append(f"gross weight {filled['gross_weight']} g")
    return (
        "\nREFERENCE RING DETAILS (measured from the real ring in the photos): "
        + ", ".join(bits)
        + ". Keep the new design in the SAME physical class — the same overall scale, "
          "band mass and metal volume. Do not turn a substantial ring into a dainty one "
          "or vice versa.\n"
    )


def build_view_prompts(
    meta: dict[str, Any],
    has_base: bool = False,
    details: Optional[RingDetails] = None,
) -> list[dict[str, str]]:
    """Build the four single-view prompts for one ring design (same meta).

    Each view carries its render ``mode`` and its ``src`` (the view whose image an
    ``edit`` re-renders from). The hero is the ``anchor``; every other view edits
    the hero so the ring stays identical while the camera moves.

    ``has_base`` = uploaded reference photos will seed the hero: the hero uses the
    spec-free ``BASE_HERO_TEMPLATE`` and its ``src`` is ``"base"`` so the renderer
    feeds the uploads in. In that flow NO view may be a text anchor — a text
    anchor would build a ring out of the randomized spec fields and ignore the
    uploads entirely — so every non-hero view is forced to edit from the hero.

    ``details`` = the reference ring's real measurements, injected into the hero
    prompt as scale context.
    """
    reference_details = _reference_details_block(details)
    out: list[dict[str, str]] = []
    for view, label, mode, src, instruction in VIEWS:
        view_instruction = instruction.format(**meta)
        if has_base:
            if view == "hero":
                mode, src = "anchor", "base"
                prompt = BASE_HERO_TEMPLATE.format(
                    view_instruction=view_instruction,
                    reference_details=reference_details, **meta)
            else:
                # Chain off the hero so every view shows the SAME uploaded-derived
                # ring (the hero is the only view that reads the uploads).
                mode, src = "edit", src or "hero"
                prompt = EDIT_TEMPLATE.format(view_instruction=view_instruction, **meta)
        elif mode == "anchor":
            prompt = VIEW_TEMPLATE.format(view_instruction=view_instruction, **meta)
        else:
            prompt = EDIT_TEMPLATE.format(view_instruction=view_instruction, **meta)
        out.append({"view": view, "label": label, "prompt": prompt, "mode": mode,
                    "src": src or ""})
    return out


class RingImageResult(BaseModel):
    status: str  # "rendered" | "not_configured" | "error"
    view: Optional[str] = None
    label: Optional[str] = None
    prompt: str
    image_url: Optional[str] = None
    filename: Optional[str] = None
    message: Optional[str] = None
    cost_usd: Optional[float] = None  # gpt-image-1 token cost for this view


class ProductSummary(BaseModel):
    """The catalog Product Summary for a GENERATED ring.

    ``style_no`` is minted here (the design is new, so it has no real SKU). The
    weights are an ESTIMATE for the new design — the image model returns pixels,
    not a bill of materials — derived by the LLM from the reference ring's real
    measurements. ``estimated`` says whether the LLM actually ran."""
    style_no: str
    ring_size: Optional[str] = None
    metal_weight: Optional[str] = None   # grams
    gross_weight: Optional[str] = None   # grams
    estimated: bool = False
    note: Optional[str] = None


class RingViewsResult(BaseModel):
    status: str  # "rendered" | "partial" | "not_configured" | "error"
    meta: dict[str, Any] = Field(default_factory=dict)
    seed: Optional[int] = None
    model: Optional[str] = None
    quality: Optional[str] = None
    views: list[RingImageResult] = Field(default_factory=list)
    cost_usd: Optional[float] = None  # summed across all rendered views
    message: Optional[str] = None
    summary: Optional[ProductSummary] = None


def new_style_no() -> str:
    """Mint a Style No. for a generated design (UUID-derived, catalog-looking)."""
    return f"RS-{uuid.uuid4().hex[:10].upper()}"


_ESTIMATE_SYSTEM = (
    "You are a jewellery production estimator. Given the measured details of a REFERENCE ring "
    "and a brief for a NEW ring derived from it, estimate the new ring's specification.\n\n"
    "The new ring re-styles the reference — at least 30% of the visible design language changes "
    "(motif geometry, silhouette, stone arrangement, gallery, proportions, metal flow) — but it "
    "stays in the SAME physical class: same overall scale, comparable band mass and metal volume. "
    "So the weights should be CLOSE to the reference, typically within about 15%, not a different "
    "class of ring.\n\n"
    "Rules:\n"
    "- ring_size: keep the reference's size unless it is missing.\n"
    "- metal_weight: grams, one or two decimals, number only (no unit).\n"
    "- gross_weight: grams, must be >= metal_weight (it includes the stones), number only.\n"
    "- If a reference value is missing, infer a plausible one for this class of ring.\n"
    "- note: one short sentence on your reasoning.\n\n"
    'Reply with ONLY a JSON object: {"ring_size": "...", "metal_weight": "...", '
    '"gross_weight": "...", "note": "..."}'
)


def estimate_product_summary(
    settings: Settings, details: Optional[RingDetails] = None
) -> ProductSummary:
    """Build the generated ring's Product Summary.

    The Style No. is always minted locally. The weights are estimated by the LLM
    from the reference ring's measurements; if the LLM is unavailable or returns
    junk we fall back to echoing the reference values (clearly marked
    ``estimated=False``) rather than inventing numbers."""
    summary = ProductSummary(style_no=new_style_no())
    filled = details.filled() if details else {}

    def fallback(note: str) -> ProductSummary:
        summary.ring_size = filled.get("ring_size")
        summary.metal_weight = filled.get("metal_weight")
        summary.gross_weight = filled.get("gross_weight")
        summary.estimated = False
        summary.note = note
        return summary

    if not filled:
        return fallback("No reference details were provided, so no weights could be estimated.")

    try:
        from app.json_utils import parse_model_json
        from app.llm_client import build_llm_client

        client = build_llm_client(settings)
        user = (
            "REFERENCE RING (measured):\n"
            + "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in filled.items())
            + "\n\nEstimate the NEW ring's specification."
        )
        raw = client.complete(_ESTIMATE_SYSTEM, user, max_tokens=400)
        data = parse_model_json(raw)

        def pick(key: str) -> Optional[str]:
            val = data.get(key)
            if val is None or str(val).strip() == "":
                return filled.get(key)
            return str(val).strip()

        summary.ring_size = pick("ring_size")
        summary.metal_weight = pick("metal_weight")
        summary.gross_weight = pick("gross_weight")
        summary.estimated = True
        note = data.get("note")
        summary.note = str(note).strip() if note else None
        return summary
    except Exception as exc:  # noqa: BLE001 - never fail a render over the estimate
        log.warning("Ring Studio weight estimate failed, echoing reference values: %s", exc)
        return fallback(f"Estimate unavailable ({exc}); showing the reference values as given.")


def _fetch_bytes(url: str) -> bytes:
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - provider URL
        return resp.read()


def _image_bytes_from_response(resp: Any) -> bytes:
    """Extract raw PNG bytes from an OpenAI images response (b64 or URL)."""
    datum = resp.data[0]
    b64 = getattr(datum, "b64_json", None)
    if b64:
        return base64.b64decode(b64)
    url = getattr(datum, "url", None)
    if url:
        return _fetch_bytes(url)
    raise RuntimeError("Image response contained no data")


def _save_png(data: bytes, name_hint: str, view: str) -> tuple[str, str]:
    safe = "".join(c for c in str(name_hint) if c.isalnum()) or "ring"
    filename = f"{safe.lower()}-{view}-{int(time.time())}-{uuid.uuid4().hex[:6]}.png"
    path = _rings_static_dir() / filename
    path.write_bytes(data)
    log.info("Ring Studio rendered %s (%d bytes)", filename, path.stat().st_size)
    # Must mirror _rings_static_dir() under the /static mount, or the SPA 404s.
    return filename, f"{_RINGS_URL_PREFIX}/{filename}"


def _render_anchor(
    settings: Settings, item: dict[str, str], name_hint: str, model: str, quality: str
) -> tuple[Optional[bytes], RingImageResult]:
    """Render the hero (anchor) view from text. Returns (image_bytes, result)."""
    view, label, prompt = item["view"], item["label"], item["prompt"]
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.images.generate(model=model, prompt=prompt, size=_IMAGE_SIZE,
                                      quality=quality, n=1)
        data = _image_bytes_from_response(resp)
        filename, url = _save_png(data, name_hint, view)
        return data, RingImageResult(status="rendered", view=view, label=label,
                                     prompt=prompt, image_url=url, filename=filename,
                                     cost_usd=_usage_cost_usd(getattr(resp, "usage", None)))
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the batch
        log.exception("Ring Studio anchor (%s) render failed", view)
        return None, RingImageResult(status="error", view=view, label=label, prompt=prompt,
                                     message=f"Render failed: {exc}")


def _render_edit(
    settings: Settings, item: dict[str, str], ref_bytes: "bytes | list[bytes]",
    name_hint: str, model: str, quality: str
) -> tuple[Optional[bytes], RingImageResult]:
    """Render a view by editing a reference image (keeps the same ring).

    ``ref_bytes`` may be a single image's bytes, or a LIST of images — the latter
    lets the hero be seeded by several uploaded views of one ring at once, which
    the model uses together as one multi-angle reference. Returns (image_bytes,
    result) so the rendered frame can itself seed a later view in the chain."""
    view, label, prompt = item["view"], item["label"], item["prompt"]
    try:
        import io

        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        if isinstance(ref_bytes, (list, tuple)):
            image_arg: Any = []
            for i, b in enumerate(ref_bytes):
                bio = io.BytesIO(b)
                bio.name = f"reference{i}.png"  # SDK uses this for the multipart filename
                image_arg.append(bio)
        else:
            image_arg = io.BytesIO(ref_bytes)
            image_arg.name = "reference.png"
        resp = client.images.edit(model=model, image=image_arg, prompt=prompt, size=_IMAGE_SIZE,
                                   quality=quality, n=1)
        data = _image_bytes_from_response(resp)
        filename, url = _save_png(data, name_hint, view)
        return data, RingImageResult(status="rendered", view=view, label=label, prompt=prompt,
                                     image_url=url, filename=filename,
                                     cost_usd=_usage_cost_usd(getattr(resp, "usage", None)))
    except Exception as exc:  # noqa: BLE001 - surface per-view
        log.exception("Ring Studio edit (%s) render failed", view)
        return None, RingImageResult(status="error", view=view, label=label, prompt=prompt,
                                     message=f"Render failed: {exc}")


def render_ring_views(
    settings: Settings,
    meta: dict[str, Any],
    seed: Optional[int] = None,
    *,
    model: str = _DEFAULT_MODEL,
    quality: str = _DEFAULT_QUALITY,
    base_images: Optional[list[bytes]] = None,
    view_keys: Optional[set[str]] = None,
    details: Optional[RingDetails] = None,
) -> RingViewsResult:
    """Render the four per-view images for one ring design, all the SAME ring.

    Views render SEQUENTIALLY: the hero renders first and becomes the reference;
    each subsequent view is produced by editing its ``src`` view's rendered image,
    so the ring (metal, stone, setting, proportions) is carried forward unchanged
    instead of being re-invented — this is what stops the top view from
    hallucinating into a different or side-on ring. ``quality`` (low/medium/high)
    trades cost for fidelity.

    ``base_images`` = raw bytes of up to four UPLOADED photos of one ring (its
    different angles). When given, the hero is rendered by EDITING all of them
    together as a single multi-angle reference — using the raw design-language
    prompt only, with no design fields injected — instead of from text, so the
    user's own ring seeds the generation; the other views chain off that hero.

    ``view_keys`` = render only this subset of views. Any kept view whose ``src``
    is dropped will error gracefully; ``{"hero"}`` has no such dep.

    If ``OPENAI_API_KEY`` is not configured this is a graceful no-op returning the
    prompts for manual use. Per-view errors are captured so the dashboard can show
    whatever succeeded.
    """
    quality = quality if quality in _QUALITIES else _DEFAULT_QUALITY
    has_base = bool(base_images)
    items = build_view_prompts(meta, has_base=has_base, details=details)
    if view_keys:
        items = [it for it in items if it["view"] in view_keys]

    if not settings.openai_api_key:
        views = [
            RingImageResult(status="not_configured", view=it["view"], label=it["label"],
                            prompt=it["prompt"])
            for it in items
        ]
        return RingViewsResult(
            status="not_configured", meta=meta, seed=seed, quality=quality, views=views,
            summary=estimate_product_summary(settings, details),
            message=("OPENAI_API_KEY is not set — image rendering is disabled. Generate the "
                     "hero prompt first, then use its image as the reference for the other "
                     "four view prompts below."),
        )

    name_hint = str(meta.get("ring_name", "ring"))

    # Render one view at a time, in list order (each view's ``src`` is guaranteed
    # to appear earlier, so its image already exists when we reach the edit).
    # We keep the rendered bytes per view so any later view can source it. The
    # uploaded base images are pre-seeded under "base" (as a list) so the hero can
    # edit from all of them at once.
    rendered_bytes: dict[str, Any] = {}
    if has_base:
        rendered_bytes["base"] = list(base_images)
    results: dict[str, RingImageResult] = {}
    for it in items:
        view, src, mode = it["view"], it.get("src") or "", it["mode"]
        # An anchor with no src renders from text; anything with a src (edit views,
        # or the hero when seeded by a base image) re-renders from that reference.
        if mode == "anchor" and not src:
            data, result = _render_anchor(settings, it, name_hint, model, quality)
        else:
            ref = rendered_bytes.get(src)
            if ref is None:
                data, result = None, RingImageResult(
                    status="error", view=view, label=it["label"], prompt=it["prompt"],
                    message=f"The '{src}' reference view failed, so this view was skipped.")
            else:
                data, result = _render_edit(settings, it, ref, name_hint, model, quality)
        rendered_bytes[view] = data
        results[view] = result

    # Preserve the authored display order (hero, top, side, front).
    views: list[RingImageResult] = [results[it["view"]] for it in items]

    rendered = sum(1 for v in views if v.status == "rendered")
    if rendered == len(views):
        overall = "rendered"
    elif rendered == 0:
        overall = "error"
    else:
        overall = "partial"
    message = None
    if overall != "rendered":
        message = next((v.message for v in views if v.status == "error" and v.message), None)
    view_costs = [v.cost_usd for v in views if v.cost_usd is not None]
    total_cost = round(sum(view_costs), 6) if view_costs else None
    # The Product Summary describes the NEW design: a freshly minted Style No. and
    # weights estimated from the reference ring's measurements.
    summary = estimate_product_summary(settings, details)
    return RingViewsResult(status=overall, meta=meta, seed=seed, model=model,
                           quality=quality, views=views, cost_usd=total_cost, message=message,
                           summary=summary)


# ─── Background render jobs ──────────────────────────────────────────────────
# Rendering four images is a ~40–60s blocking call, which trips reverse-proxy /
# gateway timeouts (504) when done in one request. Instead the API enqueues a
# job, returns its id immediately, and the SPA polls for the result. The store
# is an in-memory singleton (safe with the single-worker uvicorn deployment),
# mirroring the graph/doc job pattern used elsewhere.

@dataclass
class RingRenderJob:
    job_id: str
    status: str = "pending"  # "pending" | "running" | "completed" | "failed"
    result: Optional[RingViewsResult] = None
    error: Optional[str] = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


class RingJobStore:
    """Thread-safe in-memory store for recent ring render jobs (capped)."""

    def __init__(self, max_jobs: int = 50) -> None:
        self._jobs: dict[str, RingRenderJob] = {}
        self._order: list[str] = []
        self._max = max_jobs
        self._lock = threading.Lock()

    def create(self) -> RingRenderJob:
        with self._lock:
            job = RingRenderJob(job_id=uuid.uuid4().hex)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            if len(self._order) > self._max:
                oldest = self._order.pop(0)
                self._jobs.pop(oldest, None)
        log.info("Created ring render job %s", job.job_id)
        return job

    def get(self, job_id: str) -> Optional[RingRenderJob]:
        with self._lock:
            return self._jobs.get(job_id)


ring_job_store = RingJobStore()


def run_render_job(
    job_id: str,
    settings: Settings,
    meta: dict[str, Any],
    seed: Optional[int] = None,
    *,
    model: str = _DEFAULT_MODEL,
    quality: str = _DEFAULT_QUALITY,
    base_images: Optional[list[bytes]] = None,
    details: Optional[RingDetails] = None,
) -> None:
    """Execute a queued render, updating the job in place. Runs in a worker
    thread (via FastAPI BackgroundTasks); never raises — failures are recorded
    on the job so the poller can surface them. ``base_images`` seeds the hero from
    the user's uploaded ring photos when supplied; ``details`` are the reference
    ring's real measurements, used for prompt context and the weight estimate."""
    job = ring_job_store.get(job_id)
    if job is None:  # evicted before it ran (store is capped)
        log.warning("Ring render job %s vanished before start", job_id)
        return
    job.status = "running"
    log.info("Ring render job %s started (quality=%s)", job_id, quality)
    try:
        job.result = render_ring_views(settings, meta, seed=seed, model=model,
                                       quality=quality, base_images=base_images,
                                       details=details)
        job.status = "completed"
        # Persist to the gallery so the render survives the capped in-memory job
        # store and container restarts. Never fail the job over a store error.
        try:
            save_generation(settings, job.result, details)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ring gallery save failed for job %s: %s", job_id, exc)
        log.info("Ring render job %s completed (%s)", job_id, job.result.status)
    except Exception as exc:  # noqa: BLE001 - record, don't crash the worker
        log.exception("Ring render job %s failed", job_id)
        job.error = str(exc)[:1000]
        job.status = "failed"
    finally:
        job.completed_at = datetime.now(timezone.utc)


def build_views_zip(result: RingViewsResult) -> Optional[tuple[str, bytes]]:
    """Bundle a render's PNGs into an in-memory ZIP. Returns (filename, bytes),
    or None when nothing was rendered. Files are read only from the rings static
    dir by basename, so a tampered filename can't escape the directory."""
    import io
    import zipfile

    rings_dir = _rings_static_dir()
    rendered = [v for v in result.views if v.status == "rendered" and v.filename]
    if not rendered:
        return None
    name = "".join(c for c in str(result.meta.get("ring_name", "ring")) if c.isalnum()) or "ring"
    name = name.lower()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for v in rendered:
            path = rings_dir / Path(v.filename).name  # basename only — no traversal
            if path.exists():
                zf.write(path, arcname=f"{name}-{v.view}.png")
    buf.seek(0)
    return f"{name}-views.zip", buf.getvalue()


# ─── Gallery persistence ─────────────────────────────────────────────────────
# Every completed render is written to Postgres so the Gallery outlives the capped
# in-memory job store and container restarts. The PNGs themselves live under
# app/static/rings/generated, which docker-compose bind-mounts to a host dir — DB
# rows and image files therefore survive a rebuild together. A missing/unreachable
# DB degrades to "gallery unavailable"; it never breaks a render.

class RingGalleryError(RuntimeError):
    pass


class GalleryEntry(BaseModel):
    style_no: str
    created_at: Optional[str] = None
    ring_size: Optional[str] = None
    metal_weight: Optional[str] = None
    gross_weight: Optional[str] = None
    estimated: bool = False
    note: Optional[str] = None
    reference: dict[str, Any] = Field(default_factory=dict)  # what the user typed in
    images: list[dict[str, Any]] = Field(default_factory=list)  # [{view,label,image_url}]
    cost_usd: Optional[float] = None


class PostgresRingGallery:
    """Postgres persistence for generated rings (mirrors PostgresConversationStore)."""

    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise RingGalleryError("DATABASE_URL is required for the Ring Studio gallery")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RingGalleryError("Install psycopg[binary] to use the Ring Studio gallery") from exc
        self.settings = settings
        self._psycopg = psycopg
        self._dict_row = dict_row

    def _connect(self):
        return self._psycopg.connect(self.settings.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ring_studio_generations (
                    id BIGSERIAL PRIMARY KEY,
                    style_no TEXT NOT NULL UNIQUE,
                    ring_size TEXT,
                    metal_weight TEXT,
                    gross_weight TEXT,
                    estimated BOOLEAN NOT NULL DEFAULT FALSE,
                    note TEXT,
                    reference JSONB NOT NULL DEFAULT '{}'::jsonb,
                    images JSONB NOT NULL DEFAULT '[]'::jsonb,
                    cost_usd DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS user_id INTEGER;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS user_email TEXT;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS style_no TEXT;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS ring_size TEXT;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS metal_weight TEXT;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS gross_weight TEXT;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS estimated BOOLEAN NOT NULL DEFAULT FALSE;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS note TEXT;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS reference JSONB NOT NULL DEFAULT '{}'::jsonb;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS images JSONB NOT NULL DEFAULT '[]'::jsonb;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION;")
            conn.execute("ALTER TABLE ring_studio_generations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ring_studio_generations_created_at
                ON ring_studio_generations (created_at DESC)
                """
            )

    def save(
        self,
        result: RingViewsResult,
        details: Optional[RingDetails],
        user_id: Optional[int] = None,
        user_email: Optional[str] = None,
    ) -> None:
        summary = result.summary
        if summary is None:
            return
        images = [
            {"view": v.view, "label": v.label, "image_url": v.image_url, "filename": v.filename}
            for v in result.views if v.status == "rendered" and v.image_url
        ]
        if not images:
            return  # nothing rendered — not worth a gallery row
        self.init_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ring_studio_generations
                    (user_id, user_email, style_no, ring_size, metal_weight, gross_weight, estimated, note,
                     reference, images, cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (style_no) DO NOTHING
                """,
                (
                    user_id,
                    user_email,
                    summary.style_no, summary.ring_size, summary.metal_weight,
                    summary.gross_weight, summary.estimated, summary.note,
                    json.dumps(details.filled() if details else {}, ensure_ascii=False),
                    json.dumps(images, ensure_ascii=False),
                    result.cost_usd,
                ),
            )
        log.info("Ring gallery saved %s (%d images, user_id=%s)", summary.style_no, len(images), user_id)

    def list(self, limit: int = 100, user_id: Optional[int] = None, is_admin: bool = False) -> list[GalleryEntry]:
        self.init_schema()
        with self._connect() as conn:
            if is_admin or user_id is None:
                rows = conn.execute(
                    """
                    SELECT style_no, ring_size, metal_weight, gross_weight, estimated, note,
                           reference, images, cost_usd, created_at
                    FROM ring_studio_generations
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (max(1, min(int(limit), 500)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT style_no, ring_size, metal_weight, gross_weight, estimated, note,
                           reference, images, cost_usd, created_at
                    FROM ring_studio_generations
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (user_id, max(1, min(int(limit), 500))),
                ).fetchall()
        return [
            GalleryEntry(
                style_no=r["style_no"], ring_size=r["ring_size"],
                metal_weight=r["metal_weight"], gross_weight=r["gross_weight"],
                estimated=bool(r["estimated"]), note=r["note"],
                reference=r["reference"] or {}, images=r["images"] or [],
                cost_usd=r["cost_usd"],
                created_at=r["created_at"].isoformat() if r["created_at"] else None,
            )
            for r in rows
        ]


def save_generation(
    settings: Settings,
    result: RingViewsResult,
    details: Optional[RingDetails],
    user_id: Optional[int] = None,
    user_email: Optional[str] = None,
) -> None:
    """Persist one completed render to the gallery (best-effort)."""
    PostgresRingGallery(settings).save(result, details, user_id=user_id, user_email=user_email)


def list_generations(
    settings: Settings,
    limit: int = 100,
    user_id: Optional[int] = None,
    is_admin: bool = False,
) -> list[GalleryEntry]:
    """Every generated ring, newest first."""
    return PostgresRingGallery(settings).list(limit=limit, user_id=user_id, is_admin=is_admin)


# ─── Pydantic request/response models ────────────────────────────────────────

class PromptRequest(BaseModel):
    seed: Optional[int] = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class PromptResponse(BaseModel):
    prompt: str
    meta: dict[str, Any]
    seed: Optional[int] = None


class BatchRequest(BaseModel):
    count: int = Field(default=10, ge=1, le=1000)
    start_seed: int = 0


class ImageRequest(BaseModel):
    # Supply the ``meta`` of an already-generated design (preferred, keeps the
    # exact ring), or seed/overrides to assemble a fresh one first.
    meta: Optional[dict[str, Any]] = None
    seed: Optional[int] = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    quality: str = _DEFAULT_QUALITY  # low | medium | high


class RingJobRef(BaseModel):
    """Returned when a render is enqueued — poll /ring-studio/image/{job_id}."""
    job_id: str
    status: str


class RingJobStatus(BaseModel):
    job_id: str
    status: str  # "pending" | "running" | "completed" | "failed"
    result: Optional[RingViewsResult] = None
    error: Optional[str] = None
