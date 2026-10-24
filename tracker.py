#!/usr/bin/env python3
"""MTG booster value tracker. No fabricated market prices or stale-data publishing."""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import logging
import math
import os
import random
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

LOG = logging.getLogger("mtg-tracker")
MTGJSON = "https://mtgjson.com/api/v5"
TCGCSV = "https://tcgcsv.com/tcgplayer/1"
STATE_FILE = Path("state/message_ids.json")
USER_AGENT = "MTGBoosterValueDiscordTracker/1.0 (personal GitHub Actions project)"
MIN_COVERAGE = 0.97  # Expected number of priced card slots; tokens/ad cards excluded
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
# Sealed-product names that CANNOT be priced as an individual, unsleeved booster.
NOT_SINGLE = re.compile(r"\b(box|display|case|bundle|sleeved|blister|omega|multi.?pack|draft night|gift|3.?pack|six.?pack|sample|jumpstart)\b", re.I)
TOKEN_SLOT = re.compile(r"token|advertisement|marketing|art card|helper card", re.I)


@dataclass
class Candidate:
    code: str
    name: str
    kind: str
    group_id: int | None
    product_id: int
    product_name: str
    booster_set: str
    booster_code: str
    release_date: str


@dataclass
class Result:
    name: str
    code: str
    kind: str
    price: float
    ev: float
    median: float
    roi: float
    coverage: float
    card_price_day: str
    source_product_id: int
    profile: str = "Card profile unavailable"
    net_ev: float = 0.0
    confidence: int = 0
    risk: str = "Unknown"
    movement: str = ""


class SourceError(RuntimeError):
    pass


class HTTP:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last_tcg_call = 0.0

    def get(self, url: str, *, as_bytes: bool = False) -> Any:
        if not url.startswith((MTGJSON + "/", TCGCSV + "/", "https://tcgcsv.com/")):
            raise SourceError("Unexpected data URL")
        if urlparse(url).hostname == "tcgcsv.com":
            delay = .22 - (time.monotonic() - self.last_tcg_call)
            if delay > 0:
                time.sleep(delay)
            self.last_tcg_call = time.monotonic()
        for attempt in range(4):
            try:
                resp = self.session.get(url, timeout=(15, 90))
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** attempt * 2, 16))
                    continue
                resp.raise_for_status()
                if as_bytes:
                    return resp.content
                if url.endswith(".txt"):
                    return resp.text.strip()
                body = resp.json()
                if body.get("success") is False:
                    raise SourceError(f"Source returned unsuccessful response: {url}")
                return body
            except (requests.RequestException, ValueError) as exc:
                if attempt == 3:
                    raise SourceError(f"Cannot fetch {url}: {exc}") from exc
                time.sleep(2 ** attempt)
        raise SourceError(f"Too many retries: {url}")

    def mtgjson(self, file: str) -> dict:
        return self.get(f"{MTGJSON}/{file}")

    def tcg(self, group: int, endpoint: str) -> list:
        result = self.get(f"{TCGCSV}/{group}/{endpoint}")
        return result.get("results", [])


def positive(value: Any) -> float | None:
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError):
        return None


def price_for_uuid(prices: dict, uuid: str, foil: bool) -> tuple[float | None, str | None]:
    try:
        block = prices[uuid]["paper"]["tcgplayer"]["retail"]["foil" if foil else "normal"]
    except (KeyError, TypeError):
        return None, None
    if not isinstance(block, dict) or not block:
        return None, None
    day = max(block)
    return positive(block[day]), day


def kind_of_product(name: str) -> str | None:
    if NOT_SINGLE.search(name):
        return None
    n = name.casefold()
    if re.search(r"\bplay booster(?: pack)?\b", n):
        return "play"
    if re.search(r"\bcollector booster(?: pack)?\b", n):
        return "collector"
    return None


def select_candidates(set_list: list, today: date, min_year: int) -> list[Candidate]:
    output = []
    seen = set()
    for s in set_list:
        if s.get("isOnlineOnly") or s.get("isPartialPreview"):
            continue
        released = s.get("releaseDate", "")
        if not (f"{min_year}-01-01" <= released <= today.isoformat()):
            continue
        for sealed in s.get("sealedProduct") or []:
            kind = kind_of_product(sealed.get("name", ""))
            if not kind:
                continue
            ids = sealed.get("identifiers") or {}
            try:
                product_id = int(ids.get("tcgplayerProductId"))
            except (ValueError, TypeError):
                continue
            packs = (sealed.get("contents") or {}).get("pack") or []
            if len(packs) != 1:
                # Never guess what a product contains.
                continue
            pack = packs[0]
            booster_code = str(pack.get("code", "")).lower()
            booster_set = str(pack.get("set", "")).upper()
            if not booster_code or not booster_set:
                continue
            key = (booster_set, booster_code, kind, product_id)
            if key in seen:
                continue
            seen.add(key)
            output.append(Candidate(
                code=str(s["code"]).upper(), name=str(s["name"]), kind=kind,
                group_id=int(s["tcgplayerGroupId"]) if s.get("tcgplayerGroupId") else None,
                product_id=product_id, product_name=sealed["name"],
                booster_set=booster_set, booster_code=booster_code,
                release_date=released,
            ))
    return output


def derive_groups(groups: list, candidates: list) -> list[int]:
    # Additional similarly-named TCGplayer groups are used for alternate printings/product listings.
    result = {2576}  # Magic Sealed Product group
    def group_normalize(text: str) -> str:
        text = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
        text = re.sub(r"^(?:magic the gathering|universes beyond)\s+", "", text)
        return text

    names = {group_normalize(c.name) for c in candidates}
    for c in candidates:
        if c.group_id:
            result.add(c.group_id)
    for group in groups:
        title = str(group.get("name") or "").casefold()
        normalized = group_normalize(title)
        for needle in names:
            if needle and (normalized == needle or normalized.startswith(needle + " ") or (len(needle) > 8 and needle in normalized)):
                result.add(group["groupId"])
                break
    return sorted(result)


def sealed_prices(http: HTTP, groups: list[int], candidates: list[Candidate]) -> dict[int, float]:
    ids = {c.product_id for c in candidates}
    found: dict[int, float] = {}
    for index, group in enumerate(groups):
        # Prices are matched by MTGJSON's exact TCGplayer sealed product ID, never by fuzzy name.
        try:
            values = http.tcg(group, "prices")
        except SourceError as exc:
            LOG.warning("Skipping TCGCSV group %s: %s", group, exc)
            continue
        for item in values:
            try:
                pid = int(item.get("productId"))
            except (ValueError, TypeError):
                continue
            if pid not in ids:
                continue
            market = positive(item.get("marketPrice"))
            if market:
                # Same product may have multiple subtypes; prefer Normal or empty variants.
                subtype = str(item.get("subTypeName") or "").casefold()
                if subtype in ("normal", "", "standard", "unopened"):
                    found[pid] = market
        if (index + 1) % 10 == 0:
            LOG.info("Checked %s/%s TCGCSV groups; %s/%s products priced", index + 1, len(groups), len(found), len(ids))
    return found


def _weighted_sampling_arrays(sheet: dict, prices: dict, is_token: bool):
    vals: list[float] = []
    weights: list[float] = []
    names: list[str] = []
    covered: list[bool] = []
    latest: list[str] = []
    for uuid, w in (sheet.get("cards") or {}).items():
        weight = positive(w)
        if weight is None:
            continue
        p, day = price_for_uuid(prices, uuid, bool(sheet.get("foil")))
        covered.append(p is not None or is_token)
        vals.append(p or 0.0)
        weights.append(weight)
        names.append(uuid)
        if p is not None and day:
            latest.append(day)
    if not weights:
        raise SourceError("Booster sheet missing weights/cards")
    if sheet.get("fixed"):
        raise SourceError("Fixed-card sheets require specialized modeling")
    cumulative = []
    total = 0.0
    for w in weights:
        total += w
        cumulative.append(total)
    return vals, weights, names, covered, latest, cumulative, total


def evaluate_booster(config: dict, prices: dict, samples: int = 6000, seed: int = 5) -> tuple[float, float, float, str]:
    """Expected EV analytically; Monte Carlo median. Color-balanced sheets are approximate.

    Cards with unknown prices are zero-valued, not silently substituted with another finish.
    Abort if unpriced non-token card slots exceed MIN_COVERAGE threshold.
    """
    variants = config.get("boosters") or []
    sheets = config.get("sheets") or {}
    if not variants or not sheets:
        raise SourceError("Missing booster model")
    if samples < 100:
        raise SourceError("At least 100 simulated boosters required")
    # Color-balanced sheets are modeled by their published marginal card weights;
    # the within-pack color correlations are NOT replicated. This makes the
    # median an approximation (disclosed in the Discord footer).
    prepared = {}
    for sheet_name, sheet in sheets.items():
        prepared[sheet_name] = _weighted_sampling_arrays(sheet, prices, bool(TOKEN_SLOT.search(sheet_name)))
    variant_weights = []
    ev_total = 0.0
    covered_slots = 0.0
    card_slots = 0.0
    source_dates = []
    has_weighted_without_replacement = False
    for variant in variants:
        variant_weight = positive(variant.get("weight"))
        if variant_weight is None:
            raise SourceError("Booster variant missing probability weight")
        variant_weights.append(variant_weight)
    overall_weight = sum(variant_weights)
    if overall_weight <= 0:
        raise SourceError("Invalid booster probabilities")
    for variant, weight in zip(variants, variant_weights):
        probability = weight / overall_weight
        for sheet_name, amount in (variant.get("contents") or {}).items():
            if sheet_name not in prepared:
                raise SourceError(f"Unknown sheet: {sheet_name}")
            vals, weights, _names, covered, days, _cum, total = prepared[sheet_name]
            count = float(amount)
            if count < 0 or not count.is_integer():
                raise SourceError("Invalid pack slot count")
            if int(count) > len(weights) and not sheets[sheet_name].get("allowDuplicates", False):
                raise SourceError("Too many without-replacement card draws")
            if count > 1 and not sheets[sheet_name].get("allowDuplicates", False) and len(set(weights)) > 1:
                # Marginal weighted EV is biased when successive cards are
                # sampled without replacement; estimate EV from simulated packs.
                has_weighted_without_replacement = True
            ev_total += probability * count * sum(v * w for v, w in zip(vals, weights)) / total
            if not TOKEN_SLOT.search(sheet_name):
                card_slots += probability * count
                cov = sum(w for w, ok in zip(weights, covered) if ok) / total
                covered_slots += probability * count * cov
            source_dates.extend(days)
    if card_slots < 4:
        raise SourceError("Suspiciously few priced card slots")
    coverage = covered_slots / card_slots
    if coverage < MIN_COVERAGE:
        raise SourceError(f"Only {coverage:.1%} of card-slot draw probability has prices")
    # Source dates must not be silently more than 4 days old; handled by data freshness check.
    rng = random.Random(seed)
    variant_cumulative = []
    acc = 0.0
    for w in variant_weights:
        acc += w
        variant_cumulative.append(acc)
    simulated = []
    for _ in range(samples):
        roll = rng.random() * overall_weight
        idx = bisect.bisect_left(variant_cumulative, roll)
        variant = variants[min(idx, len(variants) - 1)]
        pack_val = 0.0
        for sheet_name, amount in (variant.get("contents") or {}).items():
            vals, _weights, names, _covered, _dates, cumulative, total = prepared[sheet_name]
            used = set()
            duplicates = sheets[sheet_name].get("allowDuplicates", False)
            for _ in range(int(amount)):
                if duplicates:
                    chosen = bisect.bisect_left(cumulative, rng.random() * total)
                else:
                    # Try the published weighted distribution first; rejection
                    # of previously chosen cards is exact conditioning.
                    chosen = bisect.bisect_left(cumulative, rng.random() * total)
                    for _retry in range(24):
                        if names[chosen] not in used:
                            break
                        chosen = bisect.bisect_left(cumulative, rng.random() * total)
                    if names[chosen] in used:
                        # Exact fallback for highly skewed sheets: bounded retries
                        # prevent failures and keep ordinary sheets efficient.
                        remaining = [(i, w) for i, w in enumerate(_weights)
                                     if names[i] not in used]
                        available_total = sum(w for _, w in remaining)
                        if available_total <= 0:
                            raise SourceError("No eligible cards left in sheet")
                        roll = rng.random() * available_total
                        running = 0.0
                        chosen = remaining[-1][0]
                        for index, weight in remaining:
                            running += weight
                            if roll < running:
                                chosen = index
                                break
                    used.add(names[chosen])
                pack_val += vals[chosen]
        simulated.append(pack_val)
    if has_weighted_without_replacement:
        ev_total = statistics.mean(simulated)
    return ev_total, statistics.median(simulated), coverage, max(source_dates) if source_dates else "unknown"



def net_price(price: float, fee: float = 0.13, minimum: float = 1.0) -> float:
    """Conservative resale proxy; not a buylist quote or guaranteed proceeds."""
    return max(0.0, price * (1 - fee)) if price >= minimum else 0.0


def model_net_ev(config: dict, prices: dict) -> float:
    variants = config.get("boosters") or []
    sheets = config.get("sheets") or {}
    total = sum(positive(v.get("weight")) or 0 for v in variants)
    if not total:
        raise SourceError("Invalid booster variant weights")
    value = 0.0
    for variant in variants:
        chance = (positive(variant.get("weight")) or 0) / total
        for name, count in (variant.get("contents") or {}).items():
            sheet = sheets.get(name)
            if not sheet:
                raise SourceError("Unknown booster sheet")
            cards = sheet.get("cards") or {}
            weights = [(positive(w) or 0, price_for_uuid(prices, uid, bool(sheet.get("foil")))[0] or 0)
                       for uid, w in cards.items()]
            denominator = sum(w for w, _ in weights)
            if not denominator:
                raise SourceError("Empty booster sheet")
            value += chance * int(count) * sum(w * net_price(p) for w, p in weights) / denominator
    return value


def risk_label(ev: float, median: float) -> str:
    ratio = median / ev if ev > 0 else 0
    return "High" if ratio < 0.5 else "Medium" if ratio < 0.8 else "Low"


def confidence_score(coverage: float, price_day: str, today: date, profile: str) -> int:
    parsed = parse_date_from_text(price_day)
    age = max(0, (today - parsed).days) if parsed else 5
    penalty = 12 if any(x in profile.lower() for x in ("partial", "limited", "unavailable")) else 0
    return max(0, min(100, round(coverage * 80 + 20 - age * 3 - penalty)))


def price_anomaly(price: float, previous: float | None = None) -> bool:
    return price < 0.50 or price > 5000 or bool(previous and (price < previous * .4 or price > previous * 2.5))


def movement_arrow(current: int, previous: int | None) -> str:
    if previous is None:
        return "NEW"
    difference = previous - current
    return f"▲{difference}" if difference > 0 else f"▼{-difference}" if difference < 0 else "—"


def load_history(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}



def audit_booster_config(config: dict) -> list[str]:
    """Surface modeling limitations instead of silently claiming pack accuracy."""
    findings = []
    variants = config.get("boosters") or []
    sheets = config.get("sheets") or {}
    if not variants or not sheets:
        return ["missing booster configuration"]
    for name, sheet in sheets.items():
        if sheet.get("fixed"):
            findings.append(f"{name}: fixed-card sheet unsupported")
        if sheet.get("balanceColors"):
            findings.append(f"{name}: color-balanced draws approximated")
        if not sheet.get("cards"):
            findings.append(f"{name}: empty card pool")
    for variant in variants:
        for name, count in (variant.get("contents") or {}).items():
            if name not in sheets:
                findings.append(f"unknown sheet: {name}")
            elif int(count) > 1 and not sheets[name].get("allowDuplicates", False):
                weights = [positive(w) for w in (sheets[name].get("cards") or {}).values()]
                if len(set(weights)) > 1:
                    findings.append(f"{name}: weighted without-replacement EV uses Monte Carlo")
    return sorted(set(findings))


def parse_date_from_text(text: str) -> date | None:
    match = re.search(r"(20\d{2})-(\d\d)-(\d\d)", text)
    if match:
        try:
            return date.fromisoformat(match.group(0))
        except ValueError:
            return None
    return None


def fresh_or_fail(source: str, source_day: date | None, today: date, max_age: int = 4) -> None:
    if source_day is None:
        raise SourceError(f"Missing date for {source}; will not claim current prices")
    if not (today - timedelta(days=max_age) <= source_day <= today + timedelta(days=1)):
        raise SourceError(f"{source} is stale or future-dated: {source_day.isoformat()}")


def short_name(name: str, max_width: int = 22) -> str:
    """Use recognizable titles instead of clipping important words."""
    aliases = {
        "murders at karlov manor": "Karlov Manor",
        "outlaws of thunder junction": "Thunder Junction",
        "avatar: the last airbender": "Avatar",
        "magic: the gathering | avatar: the last airbender": "Avatar",
        "marvel's spider-man": "Spider-Man",
        "secrets of strixhaven": "Strixhaven",
        "duskmourn: house of horror": "Duskmourn",
        "magic: the gathering—final fantasy": "Final Fantasy",
        "magic: the gathering - final fantasy": "Final Fantasy",
        "teenage mutant ninja turtles": "TMNT",
    }
    result = aliases.get(name.casefold(), name)
    result = re.sub(r"^Magic: The Gathering\s*(?:\||—|-)\s*", "", result, flags=re.I)
    if len(result) <= max_width:
        return result
    # For other long titles, prefer a whole-word abbreviation to an ellipsis.
    words = result.split()
    while len(" ".join(words)) > max_width and len(words) > 1:
        words.pop()
    candidate = " ".join(words)
    return candidate if len(candidate) <= max_width else result[:max_width]

def booster_card_profile(config: dict, set_data: dict) -> str:
    """Approximate booster-pool colors/types using published slot probabilities.

    Set files omit some Collector bonus printings. Weight known card metadata
    by expected slot appearances; explicitly mark partial coverage.
    """
    cards = {c["uuid"]: c for c in set_data.get("cards", []) if c.get("uuid")}
    variants = config.get("boosters") or []
    sheets = config.get("sheets") or {}
    weights = [positive(v.get("weight")) or 0 for v in variants]
    weight_total = sum(weights)
    if not cards or not weight_total:
        return "Mixed printings (profile unavailable)"
    total_slots = 0.0
    known_slots = 0.0
    nonlands = 0.0
    colorless = 0.0
    colors = Counter()
    tribes = Counter()
    kinds = Counter()
    for variant, vw in zip(variants, weights):
        if not vw:
            continue
        prob = vw / weight_total
        for sheet_name, count in (variant.get("contents") or {}).items():
            if TOKEN_SLOT.search(sheet_name):
                continue
            sheet = sheets.get(sheet_name) or {}
            sheet_cards = sheet.get("cards") or {}
            entries = [(uid, positive(w) or 0.0) for uid, w in sheet_cards.items()]
            denom = sum(w for _, w in entries)
            if not denom:
                continue
            quantity = prob * float(count)
            total_slots += quantity
            for uid, w in entries:
                card = cards.get(uid)
                if not card or not w:
                    continue
                draw = quantity * w / denom
                known_slots += draw
                c_types = set(card.get("types") or [])
                if "Land" in c_types or "Token" in c_types:
                    continue
                nonlands += draw
                card_colors = set(card.get("colors") or []) & set("WUBRG")
                if not card_colors:
                    colorless += draw
                for color in card_colors:
                    colors[color] += draw
                if "Creature" in c_types:
                    for tribe in set(card.get("subtypes") or []):
                        tribes[tribe] += draw
                for kind in ("Artifact", "Enchantment", "Planeswalker", "Battle"):
                    if kind in c_types:
                        kinds[kind] += draw
    coverage = known_slots / total_slots if total_slots else 0.0
    if coverage < .50 or nonlands < 1 or not colors:
        return "Mixed/bonus printings (profile limited)"
    present = [c for c in "WUBRG" if colors[c] > 0]
    label = "All 5" if len(present) == 5 else "/".join(present)
    colored_total = sum(colors.values())
    most_common_color, highest = colors.most_common(1)[0]
    if len(present) >= 3 and highest / colored_total >= 0.33:
        label += f" ({most_common_color}-lean)"
    if colorless / nonlands >= .09:
        label += " + colorless"
    # Count expected pulls rather than unique printings. Avoid overlabeling
    # tiny bonus-card sheets as the set's primary creature themes.
    ranked_tribes = [(t, w) for t, w in tribes.most_common()
                     if t != "Human" and w / nonlands >= .035]
    focus = [t for t, _ in ranked_tribes[:2]]
    if not focus and tribes.get("Human", 0) / nonlands >= .035:
        focus = ["Human"]
    extras = [(k, w) for k, w in kinds.most_common() if w / nonlands >= .09]
    if extras and len(focus) < 3:
        focus.append({"Artifact": "Artifacts", "Enchantment": "Enchantments",
                      "Planeswalker": "Planeswalkers", "Battle": "Battles"}[extras[0][0]])
    if not focus:
        focus = ["Mixed spells/creatures"]
    suffix = " (partial)" if coverage < .90 else ""
    return f"{label} | {', '.join(focus)}{suffix}"

def format_discord(kind: str, results: list[Result], tcg_date: date, card_date: date, top_n: int) -> dict:
    """One Discord embed per category: all 15 entries and full card profiles."""
    title = "PLAY" if kind == "play" else "COLLECTOR"
    by_set: dict[str, Result] = {}
    for r in results:
        if r.kind == kind and (r.code not in by_set or r.price < by_set[r.code].price):
            by_set[r.code] = r
    selected = sorted(by_set.values(), key=lambda x: (-x.roi, x.name))[:top_n]
    if len(selected) < 3:
        raise SourceError(f"Only {len(selected)} qualifying {title} packs. Preserving existing Discord posts")
    fields = []
    for rank, r in enumerate(selected, 1):
        profile = r.profile.replace(" | ", " — ", 1)
        movement = f" {r.movement}" if r.movement else ""
        name = f"{rank:02d}. {short_name(r.name, max_width=70)}{movement}"
        value = (
            f"**Pack** ${r.price:.2f} · **EV** ${r.ev:.2f} · **Median** ${r.median:.2f} · **ROI** {r.roi:.0f}%\n"
            f"**Net EV** ${r.net_ev:.2f} · **Confidence** {r.confidence}/100 · **Risk** {r.risk}\n"
            f"**Colors & cards:** {profile}"
        )
        if len(name) > 256 or len(value) > 1024:
            raise SourceError(f"Discord embed field too large for {r.name}")
        fields.append({"name": name, "value": value, "inline": False})
    if len(fields) > 25:
        raise SourceError("Discord allows at most 25 embed fields")
    embed = {
        "description": (
            f"Card prices {card_date.isoformat()} · Pack prices {tcg_date.isoformat()} · USD\n"
            "**EV** = modeled average · **Net EV** = estimated resale · **ROI** = EV / pack price"
        ),
        "color": 0x3B82F6 if kind == "play" else 0xF59E0B,
        "fields": fields,
    }
    total_chars = len(embed["description"]) + sum(
        len(field["name"]) + len(field["value"]) for field in fields
    )
    if total_chars > 6000:
        raise SourceError(f"{title} leaderboard exceeds Discord embed character limits")
    return {
        "content": f"**MTG {title} BOOSTERS — TOP {len(selected)} VALUE**",
        "embeds": [embed],
    }


def discord_preview(message: str | dict) -> str:
    """Readable full-length local preview without Discord webhook limits."""
    if isinstance(message, str):
        return message
    lines = [message["content"]]
    for embed in message.get("embeds", []):
        if embed.get("description"):
            lines.append(embed["description"])
        for field in embed.get("fields", []):
            lines.append(f"{field['name']}\n{field['value']}")
    return "\n\n".join(lines)


def publish_discord(messages: dict[str, str | dict], webhook: str, state_path: Path) -> None:
    if not re.match(r"^https://(?:discord(?:app)?\.com)/api/webhooks/\d+/[A-Za-z0-9_\-]+/?$", webhook):
        raise SourceError("Invalid Discord webhook URL")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    session = requests.Session()

    def send(method: str, url: str, payload: dict) -> requests.Response:
        for attempt in range(5):
            response = session.request(method, url, json=payload, timeout=20)
            if response.status_code == 429:
                try:
                    wait = min(float(response.json().get("retry_after", 1)), 60)
                except (ValueError, TypeError):
                    wait = 1.0
                time.sleep(wait + .2)
                continue
            if response.status_code >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            return response
        raise SourceError("Discord webhook rate-limited repeatedly")

    for kind in ("play", "collector", "status"):
        content = messages[kind]
        # Ranked posts use embeds; status remains plain text. Existing IDs are edited.
        payload = (dict(content) if isinstance(content, dict)
                   else {"content": content, "embeds": []})
        payload["allowed_mentions"] = {"parse": []}
        id_value = state.get(kind)
        if id_value:
            response = send("PATCH", f"{webhook.rstrip('/')}/messages/{id_value}", payload)
            if response.status_code == 404:
                LOG.warning("Original %s message missing; creating a replacement", kind)
            else:
                response.raise_for_status()
                LOG.info("Edited existing %s post (%s)", kind, id_value)
                continue
        response = send("POST", webhook.rstrip("/") + "?wait=true", payload)
        response.raise_for_status()
        new_id = str(response.json()["id"])
        state[kind] = new_id
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        LOG.info("Created %s post (%s); save this state file", kind, new_id)


def run(args) -> None:
    today = datetime.now(timezone.utc).date()
    http = HTTP()
    meta_sets = http.mtgjson("SetList.json")
    candidates = select_candidates(meta_sets["data"], today, args.min_year)
    LOG.info("Found %s recent single-pack sealed products mapped to booster configs", len(candidates))
    if not candidates:
        raise SourceError("No exact MTGJSON product-to-booster mappings found")
    tstamp = http.get("https://tcgcsv.com/last-updated.txt")
    tcg_date = parse_date_from_text(str(tstamp))
    fresh_or_fail("TCGCSV", tcg_date, today)
    groups = http.get(TCGCSV + "/groups").get("results", [])
    group_ids = derive_groups(groups, candidates)
    LOG.info("Checking %s TCGCSV groups (throttled to 4-5 requests/second)", len(group_ids))
    sealed = sealed_prices(http, group_ids, candidates)
    prices_file = http.get(MTGJSON + "/AllPricesToday.json.gz", as_bytes=True)
    prices_json = json.loads(gzip.decompress(prices_file))
    prices = prices_json.get("data", {})
    date_candidates = [str(prices_json.get("meta", {}).get("date", ""))]
    if not prices:
        raise SourceError("MTGJSON AllPricesToday has no data")
    card_date = parse_date_from_text(date_candidates[0])
    if card_date is None:
        # The data file may use a timestamp-shaped metadata object; if not, fail rather than claim freshness.
        raise SourceError("MTGJSON current-price file doesn't include a parseable snapshot date")
    fresh_or_fail("MTGJSON", card_date, today)
    history_path = Path("state/price_history.json")
    history = load_history(history_path)
    previous = history.get("latest", {})
    results: list[Result] = []
    loaded: dict[str, dict] = {}
    profiles_cache: dict[tuple[str, str], str] = {}
    audit_findings: dict[str, list[str]] = {}
    for item in candidates:
        cost = sealed.get(item.product_id)
        if cost is None:
            LOG.info("No market price for %s (TCGplayer product %s)", item.product_name, item.product_id)
            continue
        if item.booster_set not in loaded:
            try:
                loaded[item.booster_set] = http.mtgjson(f"{item.booster_set}.json")["data"]
            except (SourceError, KeyError) as exc:
                LOG.warning("Skipping %s: cannot load set %s (%s)", item.name, item.booster_set, exc)
                continue
        config = (loaded[item.booster_set].get("booster") or {}).get(item.booster_code)
        if config is None:
            LOG.warning("Skipping %s: booster config %s is absent", item.name, item.booster_code)
            continue
        audit_key = f"{item.booster_set}:{item.booster_code}"
        audit_findings[audit_key] = audit_booster_config(config)
        for finding in audit_findings[audit_key]:
            LOG.warning("Model audit %s: %s", audit_key, finding)
        try:
            ev, typical, cov, price_day = evaluate_booster(config, prices, samples=args.samples, seed=42)
            if price_day != "unknown":
                latest = parse_date_from_text(price_day)
                fresh_or_fail(f"Price entries for {item.name}", latest, today, max_age=5)
        except SourceError as exc:
            LOG.warning("Skipping %s %s: %s", item.name, item.kind, exc)
            continue
        profile_key = (item.booster_set, item.booster_code)
        if profile_key not in profiles_cache:
            profiles_cache[profile_key] = booster_card_profile(config, loaded[item.booster_set])
        previous_price = positive(previous.get(f"{item.kind}:{item.code}", {}).get("price"))
        if price_anomaly(cost, previous_price):
            LOG.warning("Flagged anomalous pack price for %s: $%.2f", item.name, cost)
            continue
        profile = profiles_cache[profile_key]
        results.append(Result(item.name, item.code, item.kind, cost, ev, typical,
                              ev / cost * 100, cov, price_day, item.product_id, profile,
                              model_net_ev(config, prices),
                              confidence_score(cov, price_day, today, profile),
                              risk_label(ev, typical)))
        LOG.info("Qualified %-27s %-9s price $%.2f EV $%.2f ROI %.1f%%", item.name[:27], item.kind, cost, ev, ev / cost * 100)
    for kind in ("play", "collector"):
        ranks = history.get("ranks", {}).get(kind, {})
        ordered = sorted((r for r in results if r.kind == kind), key=lambda r: (-r.roi, r.name))
        for rank, row in enumerate(ordered, 1):
            row.movement = movement_arrow(rank, ranks.get(row.code))
    messages = {kind: format_discord(kind, results, tcg_date, card_date, args.top) for kind in ("play", "collector")}
    play_count = len({r.code for r in results if r.kind == "play"})
    collector_count = len({r.code for r in results if r.kind == "collector"})
    partial_count = sum("(partial)" in r.profile or "profile limited" in r.profile or
                        "profile unavailable" in r.profile for r in results)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    messages["status"] = (
        "**MTG BOOSTER TRACKER**\n"
        f"**Market prices updated:** Cards {card_date.isoformat()} | Packs {tcg_date.isoformat()} (source snapshot dates)\n"
        f"**Bot last ran:** <t:{int(datetime.now(timezone.utc).timestamp())}:F>"
    )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for kind, content in messages.items():
        (output / f"{kind}.txt").write_text(discord_preview(content) + "\n", encoding="utf-8")
        if isinstance(content, dict):
            (output / f"{kind}.json").write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    (output / "data.json").write_text(json.dumps([x.__dict__ for x in results], indent=2) + "\n", encoding="utf-8")
    (output / "booster_audit.json").write_text(json.dumps(audit_findings, indent=2) + "\n", encoding="utf-8")
    LOG.info("Saved ranked previews to %s", output)
    snapshot = {f"{r.kind}:{r.code}": {"price": r.price, "ev": r.ev,
                "net_ev": r.net_ev, "confidence": r.confidence,
                "risk": r.risk, "roi": r.roi} for r in results}
    history["latest"] = snapshot
    history["ranks"] = {kind: {r.code: rank for rank, r in enumerate(
        sorted((x for x in results if x.kind == kind), key=lambda x: (-x.roi, x.name)), 1)}
        for kind in ("play", "collector")}
    history.setdefault("daily", {})[today.isoformat()] = snapshot
    history["daily"] = dict(sorted(history["daily"].items())[-365:])
    if args.dry_run:
        print("\n\n".join(discord_preview(messages[kind]) for kind in ("play", "collector", "status")))
    else:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not webhook:
            raise SourceError("Set DISCORD_WEBHOOK_URL repository secret before publishing")
        publish_discord(messages, webhook, Path(args.state))
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="MTG Play/Collector EV Discord leaderboard")
    p.add_argument("--dry-run", action="store_true", help="Generate previews without Discord POST/PATCH")
    p.add_argument("--min-year", type=int, default=2024)
    p.add_argument("--samples", type=int, default=6000)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--output", default="output")
    p.add_argument("--state", default=str(STATE_FILE))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(args)
    except (SourceError, requests.RequestException) as exc:
        LOG.error("No Discord messages were edited: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
