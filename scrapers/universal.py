"""
универсальный парсер для любых сайтов"""
import re
import json
from bs4 import BeautifulSoup

from utils.price import (
    parse_price_and_currency,
    is_valid_price_element,
    extract_price_from_jsonld,
)


def _iter_json_objects(obj):
    """рекурсивно итерирует JSON-подобные структуры и возвращает все вложенные dict-объекты"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_json_objects(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_objects(item)


def _normalize_condition(raw: str | None) -> str | None:
    if not raw:
        return None

    s = " ".join(str(raw).split()).strip()
    if not s:
        return None

    s_lower = s.lower()

    # отбрасываем служебные подписи и плейсхолдеры вместо состояния
    noise_values = {
        "more info",
        "more information",
        "view more",
        "show more",
        "details",
        "read more",
        "learn more",
        "mehr info",
        "mehr informationen",
        "weitere informationen",
        "piu info",
        "più info",
        "maggiori informazioni",
        "plus d'informations",
        "plus d informations",
        "condition info",
    }
    noise_key = re.sub(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ ]+", " ", s_lower)
    noise_key = " ".join(noise_key.split())
    if noise_key in noise_values:
        return None

    if "schema.org" in s_lower and "condition" in s_lower:
        key = s.rstrip("/").split("/")[-1]
        schema_map = {
            "NewCondition": "New",
            "UsedCondition": "Used",
            "RefurbishedCondition": "Refurbished",
            "DamagedCondition": "Damaged",
        }
        return schema_map.get(key, key.replace("Condition", "") or key)

    # rU -> EN быстрый маппинг
    ru_map = {
        "новый": "New",
        "новое": "New",
        "новая": "New",
        "как новое": "Like new",
        "б/у": "Used",
        "бу": "Used",
        "отличное": "Excellent",
        "очень хорошее": "Very good",
        "хорошее": "Good",
        "удовлетворительное": "Fair",
        "плохое": "Poor",
    }
    for k, v in ru_map.items():
        if k in s_lower:
            return v

    # de -> EN быстрый маппинг
    de_map = {
        "wie neu": "Like new",
        "neuwertig": "Like new",
        "neu": "New",
        "sehr guter zustand": "Very good",
        "sehr gut": "Very good",
        "guter zustand": "Good",
        "gut": "Good",
        "gebraucht": "Used",
        "akzeptabel": "Fair",
        "befriedigend": "Fair",
        "stark benutzt": "Worn",
    }
    for k, v in de_map.items():
        if k in s_lower:
            return v

    # нормализация обычных английских ценностей
    common = {
        "excellent condition": "Excellent condition",
        "very good condition": "Very good condition",
        "good condition": "Good condition",
        "fair condition": "Fair condition",
        "poor condition": "Poor condition",
        "like new": "Like new",
        "pre-loved": "Pre-loved",
        "preloved": "Pre-loved",
        "pre-owned": "Pre-owned",
        "pre owned": "Pre-owned",
        "gently used": "Gently used",
        "excellent": "Excellent",
        "very good": "Very good",
        "great condition": "Great condition",
        "good": "Good",
        "fair": "Fair",
        "poor": "Poor",
        "pristine": "Pristine",
        "mint": "Mint",
        "shows wear": "Shows wear",
        "worn": "Worn",
        "giftable": "Giftable",
        "flawed": "Flawed",
        "new": "New",
        "used": "Used",
    }
    for k, v in common.items():
        if k in s_lower:
            return v

    # по умолчанию: оставляем как есть, но обрезаем лишние пробелы
    return " ".join(s.split())[:60]


def _normalize_country(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # часто приходит "City, State, Country" — берём последний сегмент
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        if parts:
            s = parts[-1]

    s_lower = s.lower().strip()
    # популярные алиасы (чтобы было короче в сообщении)
    alias_map = {
        "usa": "US",
        "u.s.a.": "US",
        "u.s.": "US",
        "united states": "US",
        "united states of america": "US",
        "uk": "UK",
        "u.k.": "UK",
        "united kingdom": "UK",
        "great britain": "UK",
    }
    if s_lower in alias_map:
        return alias_map[s_lower]

    # iSO-2 код
    m = re.fullmatch(r"[A-Za-z]{2}", s)
    if m:
        return s.upper()

    # локализация как en_US
    m = re.fullmatch(r"[a-z]{2}[_-]([A-Z]{2})", s)
    if m:
        return m.group(1).upper()

    # оставляем название страны как есть
    if re.fullmatch(r"[A-Za-z .'-]{3,60}", s):
        return " ".join(w.capitalize() for w in s.split())[:60]

    return " ".join(s.split())[:60]


def _get_script_text(tag) -> str:
    try:
        txt = tag.string
        if txt is None:
            txt = tag.get_text()
        return txt or ""
    except Exception:
        return ""


def _try_load_json(text: str):
    if not text:
        return None
    s = text.strip()
    if not s:
        return None

    # иногда JSON-LD обёрнут в комментарии/CDATA
    if s.startswith("<!--"):
        s = s[4:]
    if s.endswith("-->"):
        s = s[:-3]
    s = s.strip()
    if s.startswith("<![CDATA["):
        s = s[len("<![CDATA["):]
    if s.endswith("]]>"):
        s = s[:-3]
    s = s.strip()

    try:
        return json.loads(s)
    except Exception:
        pass

    # fallback: попробуем вырезать основной JSON объект/массив
    first_bracket = s.find("[")
    last_bracket = s.rfind("]")
    first_curly = s.find("{")
    last_curly = s.rfind("}")

    candidate = ""
    if first_bracket != -1 and last_bracket != -1 and (first_curly == -1 or first_bracket < first_curly):
        candidate = s[first_bracket:last_bracket + 1]
    elif first_curly != -1 and last_curly != -1:
        candidate = s[first_curly:last_curly + 1]

    if candidate:
        try:
            return json.loads(candidate)
        except Exception:
            return None
    return None


def _extract_from_dt_dd(soup: BeautifulSoup, label_keywords: list[str]) -> str | None:
    try:
        for dt in soup.find_all("dt"):
            key = dt.get_text(" ", strip=True).lower()
            key = " ".join(key.split())
            if not key:
                continue
            if any(k in key for k in label_keywords):
                dd = dt.find_next_sibling("dd")
                if dd:
                    val = dd.get_text(" ", strip=True)
                    if val:
                        return val
    except Exception:
        return None
    return None


def _extract_from_th_td(soup: BeautifulSoup, label_keywords: list[str]) -> str | None:
    try:
        for th in soup.find_all("th"):
            key = th.get_text(" ", strip=True).lower()
            key = " ".join(key.split())
            if not key:
                continue
            if any(k in key for k in label_keywords):
                td = th.find_next_sibling("td")
                if td:
                    val = td.get_text(" ", strip=True)
                    if val:
                        return val
    except Exception:
        return None
    return None


def _clean_text(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _page_hint_text(soup: BeautifulSoup) -> str:
    # собираем подсказки о сайте из canonical/og мета, чтобы ограничивать эвристики доменом
    parts: list[str] = []
    try:
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            parts.append(str(canonical.get("href")))
    except Exception:
        pass

    try:
        meta_og = soup.find("meta", {"property": "og:url"})
        if meta_og and meta_og.get("content"):
            parts.append(str(meta_og.get("content")))
    except Exception:
        pass

    try:
        meta_site = soup.find("meta", {"property": "og:site_name"})
        if meta_site and meta_site.get("content"):
            parts.append(str(meta_site.get("content")))
    except Exception:
        pass

    try:
        meta_tw = soup.find("meta", {"name": "twitter:domain"})
        if meta_tw and meta_tw.get("content"):
            parts.append(str(meta_tw.get("content")))
    except Exception:
        pass

    return " ".join(parts).lower()


def _extract_condition_ebay(soup: BeautifulSoup) -> str | None:
    # извлекаем текст состояния из стандартного блока ebay
    try:
        block = soup.select_one(".x-item-condition-text")
        if not block:
            return None
        candidates = []
        for span in block.select("span.ux-textspans"):
            t = _clean_text(span.get_text(" ", strip=True))
            if not t:
                continue
            low = t.lower().strip().rstrip(":")
            if low in {"condition"}:
                continue
            candidates.append(t)

        if not candidates:
            return None

        for t in candidates:
            if _looks_like_condition(t):
                return t.strip()

        return candidates[0].strip()
    except Exception:
        return None


def _extract_condition_annsfabulousfinds(soup: BeautifulSoup) -> str | None:
    # получаем состояние из специфичного тега aff
    try:
        el = soup.select_one(".aff_star_rating")
        if not el:
            return None
        return _clean_text(el.get_text(" ", strip=True))
    except Exception:
        return None


def _looks_like_condition(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False

    # отсекаем очевидные CTA, чтобы не спутать со "состоянием"
    bad_markers = [
        "add to cart",
        "buy now",
        "checkout",
        "sold out",
        "out of stock",
        "more info",
        "mehr info",
        "piu info",
        "più info",
    ]
    if any(m in t for m in bad_markers):
        return False

    # минимальная проверка на "похоже на состояние" (best-effort)
    good_markers = [
        "new",
        "like new",
        "excellent",
        "very good",
        "good",
        "fair",
        "poor",
        "used",
        "pre-owned",
        "pre owned",
        "pre-loved",
        "preloved",
        "gently used",
        "great condition",
        "vintage",
        "pristine",
        "mint",
        "shows wear",
        "worn",
        "giftable",
        "flawed",
        "zustand",
        "wie neu",
        "gebraucht",
        "sehr gut",
        "guter zustand",
    ]
    return any(m in t for m in good_markers)


def _extract_labeled_condition_value(text: str | None) -> str | None:
    raw = _clean_text(text)
    if not raw:
        return None

    normalized = " ".join(raw.split())
    if not normalized:
        return None

    patterns = [
        r"\b(?:item\s+condition|condition|condizione|etat|état|state|zustand|artikelzustand)\b\s*[:\-]\s*([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ /'()\-]{1,60})",
        r"\b(?:item\s+condition|condition|condizione|etat|état|state|zustand|artikelzustand)\b\s+([A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ /'()\-]{1,40})",
    ]
    for pattern in patterns:
        m = re.search(pattern, normalized, flags=re.I)
        if not m:
            continue
        value = _clean_text(m.group(1))
        if not value:
            continue
        value = re.split(r"[|•·]", value, maxsplit=1)[0].strip()
        if not value:
            continue
        normalized_value = _normalize_condition(value)
        if normalized_value:
            return normalized_value.strip()

    return None


def _extract_condition_from_description_text(text: str | None) -> str | None:
    # извлекаем состояние из длинного описания по ключевым фразам
    raw = _clean_text(text)
    if not raw:
        return None

    normalized = " ".join(raw.split())
    low = normalized.lower()

    # ищем наиболее информативные маркеры состояния
    patterns = [
        (r"\b(brand\s+new|new\s+with\s+tags|new\s+without\s+tags|new\s+in\s+box|bnwt|nwt|never\s+worn|unworn|unused)\b", "New"),
        (r"\blike\s+new\b", "Like new"),
        (r"\bpre[- ]?loved\b|\bpreloved\b", "Pre-loved"),
        (r"\bgently\s+used\b", "Gently used"),
        (r"\bpre[- ]?owned\b|\bpreowned\b", "Pre-owned"),
        (r"\bexcellent(\s+condition)?\b", "Excellent"),
        (r"\bvery\s+good(\s+condition)?\b", "Very good"),
        (r"\bgreat\s+condition\b", "Great condition"),
        (r"\bgood(\s+condition)?\b", "Good"),
        (r"\bfair(\s+condition)?\b", "Fair"),
        (r"\bpoor(\s+condition)?\b", "Poor"),
        (r"\bused\b", "Used"),
    ]
    for pattern, value in patterns:
        if re.search(pattern, low, flags=re.I):
            return value.strip()

    # fallback: берем первую короткую фразу, если она похожа на состояние
    first_sentence = re.split(r"[.!?;]", normalized, maxsplit=1)[0].strip()
    if first_sentence and len(first_sentence) <= 60 and _looks_like_condition(first_sentence):
        return first_sentence.strip()

    return None


def _extract_condition_therealreal(soup: BeautifulSoup) -> str | None:
    # получаем состояние по активному маркеру шкалы the realreal
    try:
        li = soup.select_one("li[data-testid='product-condition-active']")
        if not li:
            return None
        txt = None
        inner = li.select_one(".condition__text")
        if inner:
            txt = _clean_text(inner.get_text(" ", strip=True))
        if not txt:
            txt = _clean_text(li.get_text(" ", strip=True))
        return txt.strip() if txt else None
    except Exception:
        return None


def _extract_condition_rebag(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние из выбранного пункта (rebag) или из описания в скриптах
    try:
        selected = soup.select_one("#pdp__section--conditions li.pdp__condition-item--selected")
        if selected:
            txt = _clean_text(selected.get_text(" ", strip=True))
            if txt:
                return txt.strip()

        # fallback для shop.rebag.com: condition часто есть в product-json/описании внутри script
        hint = _page_hint_text(soup)
        if "rebag" not in hint and not soup.select_one("meta[property='og:site_name'][content*='Rebag']"):
            return None

        # ищем короткое значение после "Condition:" (часто "Very good", "Excellent" и т.д.)
        pat = re.compile(r"\bCondition\b\s*:\s*([A-Za-z][A-Za-z /'-]{1,40})", flags=re.I)
        for script in soup.find_all("script"):
            t = _get_script_text(script)
            if not t or "condition" not in t.lower():
                continue
            m = pat.search(t)
            if not m:
                continue
            val = _clean_text(m.group(1))
            if val and len(val.strip()) <= 60:
                return val.strip()
    except Exception:
        return None

    return None


def _extract_condition_fashionphile(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние из термометра/подсказки (fashionphile)
    try:
        meter = soup.select_one(".fp-condition-thermometer[aria-valuetext]")
        if meter and meter.get("aria-valuetext"):
            txt = _clean_text(meter.get("aria-valuetext"))
            if txt:
                return txt.strip()

        desc = soup.select_one("#fp-condition-description")
        if desc:
            raw = _clean_text(desc.get_text(" ", strip=True))
            if raw:
                import re
                m = re.search(r"\"([^\"]{2,60})\"", raw)
                if m:
                    return m.group(1).strip()

        # fallback: явное значение рядом с "Condition:" в аккордеоне
        for sel in [
            ".fp-product__condition-accordion span.h6.fp-font-weight--regular",
            "summary .accordion__title span.h6.fp-font-weight--regular",
            "h2.accordion__title span.h6.fp-font-weight--regular",
        ]:
            el = soup.select_one(sel)
            if not el:
                continue
            txt = _clean_text(el.get_text(" ", strip=True))
            if txt:
                return txt.strip()
    except Exception:
        return None

    return None


def _extract_condition_aretrotale(soup: BeautifulSoup) -> str | None:
    # поиск состояния через структуру кнопки и текстовые стили
    try:
        # ограничиваем эвристику только страницами aretrotale (чтобы избежать ложных совпадений)
        hint = _page_hint_text(soup)
        is_aretrotale_hint = "aretrotale" in hint or bool(soup.select_one("#sg-pdp-product-title"))
        if not is_aretrotale_hint:
            return None

        fallback = None
        for btn in soup.select("div[role='button']"):
            for p in btn.find_all("p"):
                classes = " ".join(p.get("class") or [])
                style = p.get("style") or ""

                marker = f"{classes} {style}".lower()
                if "uppercase" not in marker and "text-transform: uppercase" not in marker:
                    continue

                span = p.find("span")
                if not span:
                    continue

                text = _clean_text(span.get_text(" ", strip=True))
                if not text:
                    continue
                if _looks_like_condition(text):
                    return text.strip()
                if fallback is None and len(text.strip()) <= 60:
                    fallback = text.strip()

        return fallback
    except Exception:
        return None

    return None


def _next_nonempty_sibling_text(el, max_hops: int = 5) -> str | None:
    try:
        cur = el
        for _ in range(max_hops):
            if not cur:
                return None
            cur = cur.find_next_sibling()
            if not cur:
                return None
            text = _clean_text(cur.get_text(" ", strip=True))
            if text:
                return text
    except Exception:
        return None
    return None


def _extract_condition_jolicloset(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние из блока характеристик jolicloset
    try:
        hint = _page_hint_text(soup)
        has_jolicloset_markup = bool(
            soup.select_one("#product")
            and (
                soup.select_one("a[data-remote*='itemConditions']")
                or soup.select_one("a[data-remote*='itemconditions']")
                or soup.select_one("div.options")
                or soup.select_one("div.controls")
            )
        )
        if "jolicloset" not in hint and not has_jolicloset_markup:
            return None

        root = soup.select_one("#product") or soup

        # сначала берем прямой текст из ссылки модального окна состояния
        # пример: data-remote="/en/ajax/itemConditions/3" -> "Good condition"
        for anchor in root.select("a[data-remote*='itemConditions'], a[data-remote*='itemconditions']"):
            anchor_text = _clean_text(anchor.get_text(" ", strip=True))
            if not anchor_text:
                continue
            normalized = _normalize_condition(anchor_text)
            if normalized:
                return normalized.strip()

        # затем разбираем строки "Condition: ..." / "Zustand: ..."
        labels = {"condizione", "condition", "zustand", "artikelzustand", "état", "etat"}
        for el in root.find_all(["span", "div", "p", "li"]):
            text_value = _clean_text(el.get_text(" ", strip=True))
            if not text_value:
                continue

            norm = " ".join(text_value.split())
            low = norm.lower().strip()
            low_label = low.rstrip(":").strip()

            if low_label in labels:
                value = _next_nonempty_sibling_text(el)
                if value:
                    normalized = _normalize_condition(value)
                    if normalized:
                        return normalized.strip()

                anchor = el.find("a")
                if anchor:
                    anchor_text = _clean_text(anchor.get_text(" ", strip=True))
                    normalized = _normalize_condition(anchor_text)
                    if normalized:
                        return normalized.strip()

            for label in labels:
                if low.startswith(label + ":"):
                    value = norm[len(label) + 1 :].strip()
                    normalized = _normalize_condition(value)
                    if normalized:
                        return normalized.strip()
                if low.startswith(label + " "):
                    value = norm[len(label) :].lstrip(" :-").strip()
                    normalized = _normalize_condition(value)
                    if normalized:
                        return normalized.strip()

            extracted = _extract_labeled_condition_value(norm)
            if extracted:
                return extracted.strip()

        # fallback для случаев, когда condition есть только в html-строке ссылки
        html = str(root)
        pattern = re.compile(
            r"itemconditions/\d+[^>]*>\s*(?P<value>[^<]{2,80})\s*<",
            flags=re.I,
        )
        for m in pattern.finditer(html):
            value = _clean_text(m.group("value"))
            normalized = _normalize_condition(value)
            if normalized:
                return normalized.strip()
    except Exception:
        return None

    return None


def _extract_condition_celebrityowned(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние из описания товара celebrityowned
    try:
        # ограничиваем эвристику только celebrityowned
        hint = _page_hint_text(soup)
        if "celebrityowned" not in hint:
            return None

        selectors = [
            ".product-description[itemprop='description'] .accordion-content",
            ".product-description[itemprop='description']",
            "[itemprop='description'] .accordion-content",
            "[itemprop='description']",
            "#shopify-section-products_tabs .accordion-content",
        ]
        for sel in selectors:
            for el in soup.select(sel):
                txt = _clean_text(el.get_text(" ", strip=True))
                if not txt:
                    continue
                cond = _extract_condition_from_description_text(txt)
                if cond:
                    return cond.strip()
    except Exception:
        return None

    return None


def _rank_to_condition(rank: str | None) -> str | None:
    # нормализуем ранги состояния в читаемое значение
    if not rank:
        return None
    r = re.sub(r"[^A-Za-z]", "", str(rank)).upper().strip()
    if not r:
        return None

    # шкала dallasdesignerhandbags
    rank_map = {
        "A": "Mint",
        "AB": "Excellent",
        "B": "Gently used",
        "BC": "Used",
        "C": "Well used",
        "CD": "Very well used",
        "D": "Need repair",
        # совместимость с возможными альтернативными рангами
        "S": "New",
        "SA": "Like new",
    }
    return rank_map.get(r)


def _extract_condition_dallasdesignerhandbags(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние из явного поля condition или из блока condition details
    try:
        # ограничиваем эвристику только dallasdesignerhandbags
        hint = _page_hint_text(soup)
        if "dallasdesignerhandbags" not in hint:
            return None

        # кейс 1: отдельное поле CONDITION + значение рядом
        for block in soup.select(".tags_condition, div[class*='tags_condition']"):
            spans = block.select("span")
            if spans:
                for i, sp in enumerate(spans):
                    label = _clean_text(sp.get_text(" ", strip=True))
                    if not label:
                        continue
                    if label.lower().strip().rstrip(":") != "condition":
                        continue

                    for nxt in spans[i + 1:]:
                        value = _clean_text(nxt.get_text(" ", strip=True))
                        if not value:
                            continue
                        if re.fullmatch(r"[-|:/]+", value):
                            continue
                        if value.lower().strip().rstrip(":") == "condition":
                            continue
                        normalized = _normalize_condition(value)
                        if normalized:
                            return normalized.strip()

            block_text = _clean_text(block.get_text(" ", strip=True))
            if block_text:
                m = re.search(r"\bcondition\b\s*[:\-]?\s*([A-Za-z][A-Za-z /'-]{1,60})", block_text, flags=re.I)
                if m:
                    normalized = _normalize_condition(m.group(1))
                    if normalized:
                        return normalized.strip()

        # кейс 2: condition details с rank (вкладка description)
        description_root = soup.select_one("#srtab-description") or soup

        heading = None
        for h in description_root.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b", "p", "span"]):
            t = _clean_text(h.get_text(" ", strip=True))
            if not t:
                continue
            low = " ".join(t.split()).lower()
            if "condition details" in low:
                heading = h
                break

        details_text = None
        if heading:
            # берем ближайший список/блок с деталями после заголовка
            for nxt in heading.find_all_next(["ul", "ol", "p", "div"], limit=8):
                txt = _clean_text(nxt.get_text(" ", strip=True))
                if not txt:
                    continue
                if "condition details" in txt.lower():
                    continue
                details_text = txt
                break

        if not details_text:
            root_text = _clean_text(description_root.get_text(" ", strip=True))
            if root_text and "condition details" in root_text.lower():
                details_text = root_text

        if details_text:
            # сначала пробуем rank, потому что это самый структурный сигнал
            rank_match = re.search(r"\brank\s*([A-Za-z]{1,2})\b", details_text, flags=re.I)
            if rank_match:
                mapped = _rank_to_condition(rank_match.group(1))
                if mapped:
                    return mapped.strip()

            parsed = _extract_condition_from_description_text(details_text)
            if parsed:
                return parsed.strip()
    except Exception:
        return None

    return None


def _extract_condition_designerexchange(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние из блока condition на designerexchange
    try:
        # ограничиваем эвристику только designerexchange
        hint = _page_hint_text(soup)
        if "designerexchange" not in hint:
            return None

        blocks = soup.select(".product-condition-container, div[class*='product-condition-container']")
        if not blocks:
            blocks = [soup]

        for block in blocks:
            heading = None
            for h in block.find_all(["h2", "h3", "h4", "h5", "strong", "span", "p"]):
                t = _clean_text(h.get_text(" ", strip=True))
                if not t:
                    continue
                if " ".join(t.split()).lower().rstrip(":").strip() == "condition":
                    heading = h
                    break

            # если нашли заголовок condition, сначала читаем соседний текст
            if heading:
                candidate = _next_nonempty_sibling_text(heading)
                if candidate:
                    parsed = _extract_condition_from_description_text(candidate)
                    if parsed:
                        return parsed.strip()
                    normalized = _normalize_condition(candidate)
                    if normalized and len(candidate.split()) <= 5:
                        return normalized.strip()

            # основной кейс сайта: описание в p внутри контейнера
            for p in block.select("p.m-b-15, p"):
                txt = _clean_text(p.get_text(" ", strip=True))
                if not txt:
                    continue
                parsed = _extract_condition_from_description_text(txt)
                if parsed:
                    return parsed.strip()

            # fallback: пробуем извлечь из текста всего блока
            block_text = _clean_text(block.get_text(" ", strip=True))
            if block_text and "condition" in block_text.lower():
                parsed = _extract_condition_from_description_text(block_text)
                if parsed:
                    return parsed.strip()
    except Exception:
        return None

    return None


def _extract_condition_theluxurycloset(soup: BeautifulSoup) -> str | None:
    # извлекаем состояние по активному индикатору selectedindex
    try:
        # ограничиваем эвристику только tlc или явными маркерами их верстки
        hint = _page_hint_text(soup)
        has_tlc_markup = bool(
            soup.select_one("[class*='ItemCondition__selectedIndex']")
            or soup.select_one("[class*='ItemCondition__base']")
            or soup.select_one("[class*='SppButtonWrapper__soldOuttitle']")
            or soup.select_one("[class*='ProductPriceV2__newProductPrice']")
        )
        if "theluxurycloset" not in hint and "the luxury closet" not in hint and not has_tlc_markup:
            return None

        # основной кейс: активное значение помечено классом selectedindex
        selectors = [
            "#root > div:nth-child(2) > div > div > div > div.DesktopWidth__base___3ZRAa > div > div.NewSPPAdditionalDetailsDesktopComponent__base___3m7G3 > div.NewSPPAdditionalDetailsDesktopComponent__bottom___OiM-M > div.ItemCondition__base___3WCLo > div > div.ItemCondition__progressBarLabelBase___4RCXO > div > div.ItemCondition__selectedIndex___2okR2",
            "[class*='ItemCondition__selectedIndex']",
            "[class*='ItemCondition__base'] [class*='selectedIndex']",
            "[class*='ItemCondition__progressBarLabelBase'] [class*='selectedIndex']",
        ]
        for sel in selectors:
            for el in soup.select(sel):
                txt = _clean_text(el.get_text(" ", strip=True))
                if not txt:
                    continue
                normalized = _normalize_condition(txt)
                if normalized:
                    return normalized.strip()
                return txt.strip()

        # fallback: ищем любой элемент с классом itemcondition и selectedindex
        for el in soup.find_all(True):
            classes = " ".join(el.get("class") or [])
            low = classes.lower()
            if "itemcondition" not in low or "selectedindex" not in low:
                continue
            txt = _clean_text(el.get_text(" ", strip=True))
            if not txt:
                continue
            normalized = _normalize_condition(txt)
            if normalized:
                return normalized.strip()
            return txt.strip()

        # fallback: иногда значение лежит только в html без нормального dom-узла
        html = str(soup)
        html_match = re.search(
            r"ItemCondition__selectedIndex[^>]*>\s*(?P<value>[^<]{2,40})\s*<",
            html,
            flags=re.I,
        )
        if html_match:
            value = _clean_text(html_match.group("value"))
            normalized = _normalize_condition(value)
            if normalized:
                return normalized.strip()

        # fallback: читаем состояние из inline-json/скриптов
        script_patterns = [
            re.compile(r'"itemCondition"\s*:\s*"(?P<value>[^"]{2,40})"', flags=re.I),
            re.compile(r'"condition"\s*:\s*"(?P<value>[^"]{2,40})"', flags=re.I),
            re.compile(r'"selectedIndex"\s*:\s*"(?P<value>[^"]{2,40})"', flags=re.I),
        ]
        for script in soup.find_all("script"):
            text = _get_script_text(script)
            if not text:
                continue
            low = text.lower()
            if "condition" not in low and "selectedindex" not in low:
                continue
            for pattern in script_patterns:
                for match in pattern.finditer(text):
                    value = _clean_text(match.group("value"))
                    normalized = _normalize_condition(value)
                    if normalized:
                        return normalized.strip()

        # fallback: если нашли блок condition, берем короткое значение из подписи
        for root in soup.select("[class*='ItemCondition__base'], [class*='ItemCondition__progressBarLabelBase']"):
            for cand in root.find_all(["div", "span", "p"]):
                txt = _clean_text(cand.get_text(" ", strip=True))
                if not txt:
                    continue
                if len(txt) > 30:
                    continue
                if _looks_like_condition(txt):
                    normalized = _normalize_condition(txt)
                    if normalized:
                        return normalized.strip()
                    return txt.strip()
    except Exception:
        return None

    return None


def _extract_condition_poshmark(soup: BeautifulSoup) -> str | None:
    # извлекает condition из описания poshmark
    try:
        # сохраняем этот парсер чтобы охватывал poshmark
        hint = _page_hint_text(soup)
        has_poshmark_markup = bool(
            soup.select_one(".listing__description")
            or soup.select_one("div[class*='listing__description']")
            or soup.select_one("meta[property='og:site_name'][content*='Poshmark']")
        )
        if "poshmark" not in hint and not has_poshmark_markup:
            return None

        selectors = [
            ".listing__description",
            "div[class*='listing__description']",
        ]
        for sel in selectors:
            for el in soup.select(sel):
                txt = _clean_text(el.get_text(" ", strip=True))
                if not txt:
                    continue

                # общий парсер описаний
                parsed = _extract_condition_from_description_text(txt)
                if parsed:
                    return parsed.strip()

                # fallback: 'отличное состояние'
                m = re.search(
                    r"\b([A-Za-z][A-Za-z /'-]{1,25})\s+condition\b",
                    txt,
                    flags=re.I,
                )
                if m:
                    normalized = _normalize_condition(m.group(1))
                    if normalized:
                        return normalized.strip()

                # fallback очень хорошее состояние
                extracted = _extract_labeled_condition_value(txt)
                if extracted:
                    return extracted.strip()

        # последний fallback: сканируем весь текст страницы чтобы явно найти 'Condition: ...'
        page_text = _clean_text(soup.get_text(" ", strip=True))
        extracted = _extract_labeled_condition_value(page_text)
        if extracted:
            return extracted.strip()
    except Exception:
        return None

    return None


def _extract_condition_vestiaire(soup: BeautifulSoup) -> str | None:
    # извлекаем condition из атрибутов
    try:
        # сохраняем этот парсер чтобы охватывал vestiaire
        is_vestiaire_hint = bool(
            soup.select_one("[data-cy='productTitle_name']") or soup.select_one("[data-cy='pdp_buy_btn']")
        )
        if not is_vestiaire_hint:
            canonical_href = ""
            try:
                canonical = soup.find("link", rel="canonical")
                canonical_href = canonical.get("href") if canonical else ""
            except Exception:
                canonical_href = ""

            og_url = ""
            try:
                meta_og = soup.find("meta", {"property": "og:url"})
                og_url = meta_og.get("content") if meta_og else ""
            except Exception:
                og_url = ""

            site_name = ""
            try:
                meta_site = soup.find("meta", {"property": "og:site_name"})
                site_name = meta_site.get("content") if meta_site else ""
            except Exception:
                site_name = ""

            hint = f"{canonical_href} {og_url} {site_name}".lower()
            if "vestiaire" not in hint:
                return None

        labels = {"condition", "item condition", "condizione", "etat", "état", "state"}

        for sel in [
            "[data-cy*='condition']",
            "[data-testid*='condition']",
            "[class*='condition']",
            "[class*='Condition']",
            "[data-cy*='state']",
            "[data-testid*='state']",
        ]:
            for el in soup.select(sel):
                txt = _clean_text(el.get_text(" ", strip=True))
                if not txt:
                    continue

                extracted = _extract_labeled_condition_value(txt)
                if extracted:
                    return extracted.strip()

                parsed = _extract_condition_from_description_text(txt)
                if parsed:
                    return parsed.strip()

        for li in soup.select("section ul li"):
            label_el = None
            for cand in li.find_all(["span", "div", "p"]):
                t = _clean_text(cand.get_text(" ", strip=True))
                if not t:
                    continue
                low = " ".join(t.split()).lower().rstrip(":").strip()
                if low in labels:
                    label_el = cand
                    break

            if not label_el:
                continue

            value = _next_nonempty_sibling_text(label_el)
            if not value and getattr(label_el, "parent", None):
                value = _next_nonempty_sibling_text(label_el.parent)
            if value:
                normalized = _normalize_condition(value)
                if normalized:
                    return normalized.strip()
                return value.strip()

            li_text = _clean_text(li.get_text(" ", strip=True))
            if not li_text:
                continue

            li_low = li_text.lower()
            for label in labels:
                idx = li_low.find(label)
                if idx == -1:
                    continue
                tail = li_text[idx + len(label):].lstrip(" :-").strip()
                if tail:
                    normalized = _normalize_condition(tail)
                    if normalized:
                        return normalized.strip()
                    return tail.strip()

        page_text = _clean_text(soup.get_text(" ", strip=True))
        extracted = _extract_labeled_condition_value(page_text)
        if extracted:
            return extracted.strip()
    except Exception:
        return None

    return None


def extract_condition_and_country(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """
    пытается извлечь состояние (condition) и страну (country) из HTML страницы товара

    возвращает: (condition, country)
"""
    condition = None
    country = None

    # извлекаем condition через site-specific блоки (высокий приоритет, т.к. дает более точные значения)
    if condition is None:
        condition = _extract_condition_ebay(soup)
    if condition is None:
        condition = _extract_condition_annsfabulousfinds(soup)
    if condition is None:
        condition = _extract_condition_aretrotale(soup)
    if condition is None:
        condition = _extract_condition_therealreal(soup)
    if condition is None:
        condition = _extract_condition_rebag(soup)
    if condition is None:
        condition = _extract_condition_fashionphile(soup)
    if condition is None:
        condition = _extract_condition_celebrityowned(soup)
    if condition is None:
        condition = _extract_condition_dallasdesignerhandbags(soup)
    if condition is None:
        condition = _extract_condition_designerexchange(soup)
    if condition is None:
        condition = _extract_condition_theluxurycloset(soup)
    if condition is None:
        condition = _extract_condition_poshmark(soup)
    if condition is None:
        condition = _extract_condition_jolicloset(soup)
    if condition is None:
        condition = _extract_condition_vestiaire(soup)

    # 1) jSON-LD (часто содержит itemCondition / addressCountry)
    try:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            data = _try_load_json(_get_script_text(script))
            if data is None:
                continue

            for obj in _iter_json_objects(data):
                if not isinstance(obj, dict):
                    continue

                # состояние
                if condition is None:
                    val = obj.get("itemCondition") or obj.get("condition") or obj.get("conditionDescription")
                    if isinstance(val, dict):
                        val = val.get("name") or val.get("@id") or val.get("url")
                    if isinstance(val, str):
                        condition = _normalize_condition(val)

                # страна
                if country is None:
                    val = obj.get("addressCountry") or obj.get("countryOfOrigin")
                    if isinstance(val, dict):
                        val = val.get("name") or val.get("@id") or val.get("url")
                    if isinstance(val, str):
                        country = _normalize_country(val)

                if condition and country:
                    break
            if condition and country:
                break
    except Exception:
        pass

    # 1.1) meta (иногда кладут condition/country напрямую)
    if condition is None:
        try:
            meta = soup.find("meta", attrs={"itemprop": re.compile(r"(item)?condition", re.I)})
            if meta and meta.get("content"):
                condition = _normalize_condition(meta.get("content"))
        except Exception:
            pass

    if country is None:
        try:
            meta = soup.find("meta", attrs={"name": re.compile(r"(geo\\.)?country", re.I)})
            if meta and meta.get("content"):
                country = _normalize_country(meta.get("content"))
        except Exception:
            pass

    # 1.2) dt/dd, th/td (часто встречается в карточках товара)
    if condition is None:
        raw = _extract_from_dt_dd(soup, ["condition", "item condition"])
        if not raw:
            raw = _extract_from_th_td(soup, ["condition", "item condition"])
        if raw:
            condition = _normalize_condition(raw)

    if country is None:
        raw = _extract_from_dt_dd(soup, ["country", "location", "ships from", "shipping from", "located in"])
        if not raw:
            raw = _extract_from_th_td(soup, ["country", "location", "ships from", "shipping from", "located in"])
        if raw:
            country = _normalize_country(raw)

    # 2) подсказки Shopify: ссылка на страну покупателя или Shopify.country
    if country is None:
        try:
            el = soup.find(attrs={"buyer-country": True})
            if el and el.get("buyer-country"):
                country = _normalize_country(el.get("buyer-country"))
        except Exception:
            pass

    if country is None:
        try:
            for script in soup.find_all("script"):
                text = script.string or script.get_text() or ""
                m = re.search(r'Shopify\\.country\\s*=\\s*\"([A-Z]{2})\"', text)
                if m:
                    country = _normalize_country(m.group(1))
                    break
        except Exception:
            pass

    if country is None:
        try:
            meta = soup.find("meta", {"property": "og:locale"})
            if meta and meta.get("content"):
                country = _normalize_country(meta.get("content"))
        except Exception:
            pass

    page_text = ""
    try:
        page_text = soup.get_text(" ", strip=True)
    except Exception:
        page_text = ""

    if condition is None and page_text:
        try:
            m = re.search(r"\bCondition\b\s*[:\-]\s*([A-Za-z][A-Za-z0-9 /'-]{1,60})", page_text, flags=re.I)
            if m:
                condition = _normalize_condition(m.group(1))
        except Exception:
            pass

    if country is None and page_text:
        # eBay-like паттерны: "Located in: City, State, United States"
        m = re.search(r"\bLocated in\b\s*[:\-]\s*([A-Za-z ,.'-]{3,80})", page_text, flags=re.I)
        if m:
            parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
            if parts:
                country = _normalize_country(parts[-1])

    if country is None and page_text:
        # базовый паттерн: "Ships from: United States"
        m = re.search(r"\bShips from\b\s*[:\-]\s*([A-Za-z ,.'-]{3,80})", page_text, flags=re.I)
        if m:
            country = _normalize_country(m.group(1))

    return condition, country


def scrape_universal(soup: BeautifulSoup) -> tuple[str | None, float | None, str | None, str | None]:
    # универсальный парсер, который работает с любой страницей
    title = None
    price = None
    currency = None
    status = "в продаже"

    # 1. json-ld структурированные данные (самый надежный способ)
    # используем улучшенную функцию для поиска цены во всех возможных полях
    try:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "{}")
                # извлекаем title и status из json-ld
                if isinstance(data, list):
                    for item in data:
                        if isinstance(
                            item,
                            dict,
                        ) and (item.get("@type") == "Product" or "Product" in (item.get("@type") or [])):
                            if not title and item.get("name"):
                                title = item.get("name")
                            offer = item.get("offers")
                            if isinstance(offer, dict):
                                avail = (offer.get("availability") or "").lower()
                                if "outofstock" in avail or "sold" in avail or "unavailable" in avail:
                                    status = "Продано"
                elif isinstance(
                    data,
                    dict,
                ) and (data.get("@type") == "Product" or "Product" in (data.get("@type") or [])):
                    if not title and data.get("name"):
                        title = data.get("name")
                    offer = data.get("offers")
                    if isinstance(offer, dict):
                        avail = (offer.get("availability") or "").lower()
                        if "outofstock" in avail or "sold" in avail or "unavailable" in avail:
                            status = "Продано"

                # используем улучшенную функцию для извлечения цены
                jsonld_price, jsonld_currency = extract_price_from_jsonld(data)
                if jsonld_price is not None:
                    price = jsonld_price
                    currency = jsonld_currency or currency
            except Exception:
                continue
    except Exception:
        pass

    # 2. meta теги
    if not price:
        for meta_sel in [
            {"itemprop": "price"},
            {"property": "product:price:amount"},
            {"property": "og:price:amount"},
            {"name": "price"},
        ]:
            meta = soup.find("meta", meta_sel)
            if meta and meta.get("content"):
                try:
                    price = float(meta.get("content").replace(",", ""))
                    break
                except Exception:
                    pass

    if not currency:
        for meta_sel in [
            {"itemprop": "priceCurrency"},
            {"property": "product:price:currency"},
            {"property": "og:price:currency"},
            {"name": "currency"},
        ]:
            meta = soup.find("meta", meta_sel)
            if meta and meta.get("content"):
                currency = meta.get("content").upper()
                break

    # 3. поиск по селекторам с ценой
    if not price:
        price_selectors = [
            '[itemprop="price"]',
            '[data-test*="price"]',
            '[class*="price"]',
            '[class*="Price"]',
            '[id*="price"]',
            '[id*="Price"]',
            'span[class*="amount"]',
            'div[class*="amount"]',
        ]
        for sel in price_selectors:
            els = soup.select(sel)
            for el in els[:10]:  # ограничиваем количество проверок
                text = el.get_text(" ", strip=True)
                p, c = parse_price_and_currency(text)
                if p is not None:
                    price = p
                    if c:
                        currency = c
                    break
            if price:
                break

    # 4. агрессивный поиск по тексту страницы (последний резерв)
    if not price:
        page_text = soup.get_text(" ", strip=True)
        # ищем паттерны вида $1234, €1234, £1234, usd 1234, 1234 usd
        patterns = [
            r'[\$€£]\s*([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?)',
            r'([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?)\s*(USD|EUR|GBP|CAD|AUD)',
            r'(USD|EUR|GBP|CAD|AUD)\s*([0-9]{1,3}(?:[,\s][0-9]{3})*(?:\.[0-9]{2})?)',
        ]
        for pattern in patterns:
            matches = re.finditer(pattern, page_text, re.IGNORECASE)
            for match in list(matches)[:5]:  # берем первые 5 совпадений
                match_text = match.group(0)
                if not is_valid_price_element(match_text):
                    continue
                try:
                    parsed_price, parsed_currency = parse_price_and_currency(match_text)
                    if parsed_price is not None and parsed_price > 0:
                        price = parsed_price
                        currency = parsed_currency or currency
                        break
                except Exception:
                    continue
            if price:
                break

    # 5. title из og:title или h1
    if not title:
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title and og_title.get("content"):
            title = og_title.get("content").strip()
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)

    # 6. статус по ключевым словам
    page_text_lower = soup.get_text(" ", strip=True).lower()
    if any(x in page_text_lower for x in ["sold", "sold out", "out of stock", "unavailable", "продано"]):
        status = "Продано"
    elif any(x in page_text_lower for x in ["add to cart", "buy now", "in stock", "available", "в наличии"]):
        status = "в продаже"

    return title, price, currency, status

