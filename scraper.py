"""
Coverage checker and plan provider.
Coverage is resolved from Postgres tables (populated via seed_db.py and sync_nio_ceps.py).
Address/city resolution uses ViaCEP, with OpenCEP as a fallback.
"""

import json
import re
import unicodedata
import urllib.request
import urllib.error

from db import cep_has_coverage, city_is_promo

RJ_CEP_MIN = 20000000
RJ_CEP_MAX = 28999999


def _normalize(text: str) -> str:
    """Uppercase + strip accents for city name comparison."""
    return unicodedata.normalize("NFD", text.upper()).encode("ascii", "ignore").decode().strip()


def _is_rj_cep(cep: str) -> bool:
    """RJ state CEPs range from 20000-000 to 28999-999."""
    try:
        return RJ_CEP_MIN <= int(cep) <= RJ_CEP_MAX
    except ValueError:
        return False


def _clean_cep(cep: str) -> str:
    """Remove all non-digit characters and zero-pad to 8 digits."""
    digits = re.sub(r"\D", "", cep)
    return digits.zfill(8)


def _empty_address() -> dict:
    return {"street": "", "neighborhood": "", "city": "", "state": ""}


def _address_from_payload(data: dict) -> dict:
    """Map ViaCEP/OpenCEP fields to the API's stable address shape."""
    return {
        "street": data.get("logradouro", "") or "",
        "neighborhood": data.get("bairro", "") or "",
        "city": data.get("localidade", "") or "",
        "state": data.get("uf", "") or "",
    }


def _fetch_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mcc-back/2.2"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _fetch_address(cep: str) -> dict:
    """Resolve every CEP via ViaCEP, falling back to OpenCEP if necessary."""
    cep_digits = _clean_cep(cep)
    cep_formatted = f"{cep_digits[:5]}-{cep_digits[5:]}"
    viacep = _fetch_json(f"https://viacep.com.br/ws/{cep_digits}/json/")
    if viacep and not viacep.get("erro") and viacep.get("localidade"):
        return _address_from_payload(viacep)

    opencep = _fetch_json(f"https://opencep.com/v1/{cep_formatted}")
    if opencep and not opencep.get("erro") and not opencep.get("error"):
        return _address_from_payload(opencep)
    return _empty_address()


# ---------------------------------------------------------------------------
# Static plan definitions
# ---------------------------------------------------------------------------

CLARO_PLANS = [
    {
        "id": "claro-350",
        "rankingPosition": None,
        "providerName": "Claro",
        "providerSlug": "claro",
        "providerLogo": "/logos/providers/claro.svg",
        "planName": "Claro 350 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 350,
        "downloadLabel": "350 Mega",
        "uploadSpeed": 175,
        "price": 79.90,
        "priceLabel": "R$ 79,90/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 958.80,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [],
    },
    {
        "id": "claro-600",
        "rankingPosition": None,
        "providerName": "Claro",
        "providerSlug": "claro",
        "providerLogo": "/logos/providers/claro.svg",
        "planName": "Claro 600 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 600,
        "downloadLabel": "600 Mega",
        "uploadSpeed": 300,
        "price": 99.90,
        "priceLabel": "R$ 99,90/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 1198.80,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [
            {"name": "Globoplay", "logo": "/logos/streaming/globoplay.png"},
        ],
    },
    {
        "id": "claro-750",
        "rankingPosition": None,
        "providerName": "Claro",
        "providerSlug": "claro",
        "providerLogo": "/logos/providers/claro.svg",
        "planName": "Claro 750 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 750,
        "downloadLabel": "750 Mega",
        "uploadSpeed": 375,
        "price": 119.90,
        "priceLabel": "R$ 119,90/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 1438.80,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [
            {"name": "Globoplay", "logo": "/logos/streaming/globoplay.png"},
        ],
    },
]

TIM_PLANS = [
    {
        "id": "tim-500",
        "rankingPosition": None,
        "providerName": "TIM",
        "providerSlug": "tim",
        "providerLogo": "/logos/providers/tim.svg",
        "planName": "TIM Live 500 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 500,
        "downloadLabel": "500 Mega",
        "uploadSpeed": 250,
        "price": 99.90,
        "priceLabel": "R$ 99,90/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 1198.80,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [],
    },
    {
        "id": "tim-700",
        "rankingPosition": None,
        "providerName": "TIM",
        "providerSlug": "tim",
        "providerLogo": "/logos/providers/tim.svg",
        "planName": "TIM Live 700 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 700,
        "downloadLabel": "700 Mega",
        "uploadSpeed": 350,
        "price": 104.90,
        "priceLabel": "R$ 104,90/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 1258.80,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [
            {"name": "Globoplay", "logo": "/logos/streaming/globoplay.png"},
        ],
    },
]

NIO_PLANS = [
    {
        "id": "nio-500",
        "rankingPosition": None,
        "providerName": "Nio",
        "providerSlug": "nio",
        "providerLogo": "/logos/providers/nio.svg",
        "planName": "Nio 500 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 500,
        "downloadLabel": "500 Mega",
        "uploadSpeed": 250,
        "price": 100.00,
        "priceLabel": "R$ 100,00/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 1200.00,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [],
    },
    {
        "id": "nio-700",
        "rankingPosition": None,
        "providerName": "Nio",
        "providerSlug": "nio",
        "providerLogo": "/logos/providers/nio.svg",
        "planName": "Nio 700 Mega",
        "promoted": False,
        "badges": ["Fibra Óptica"],
        "downloadSpeed": 700,
        "downloadLabel": "700 Mega",
        "uploadSpeed": 350,
        "price": 130.00,
        "priceLabel": "R$ 130,00/mês",
        "priceExtraInfo": ["Modem WiFi incluso", "Serviços inclusos"],
        "setupFee": "",
        "totalAnnualPrice": 1560.00,
        "technology": "Fibra Óptica",
        "technologyValue": "fiber",
        "contractDuration": None,
        "breakFee": None,
        "providerRating": None,
        "ratingCount": None,
        "streamingServices": [
            {"name": "Globoplay", "logo": "/logos/streaming/globoplay.png"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Main search logic
# ---------------------------------------------------------------------------

def search_plans(cep: str, number: str) -> dict:
    """Resolve coverage from Postgres + static plan cards."""
    cep_clean = _clean_cep(cep)
    if len(cep_clean) != 8:
        return {"error": "CEP deve ter 8 dígitos", "plans": []}

    has_claro = cep_has_coverage(cep_clean, "ceps_claro")
    has_tim = cep_has_coverage(cep_clean, "ceps_tim")
    nio_coverage = cep_has_coverage(cep_clean, "ceps_nio")

    if not has_claro and not has_tim and not nio_coverage:
        return {"error": "CEP sem cobertura", "plans": []}

    # ViaCEP identifies the city used by the Claro Promo campaign.
    address = _fetch_address(cep_clean)

    city_normalized = _normalize(address["city"]) if address["city"] else ""
    claro_promo = has_claro and bool(city_normalized) and city_is_promo(city_normalized)

    plans: list[dict] = []

    if has_claro:
        for p in CLARO_PLANS:
            plan = dict(p)
            if claro_promo:
                plan["providerSlug"] = "claro-promo"
                plan["providerName"] = "Claro Promo"
                plan["id"] = plan["id"].replace("claro-", "claro-promo-")
                plan["promoted"] = True
            plans.append(plan)

    if has_tim:
        plans += [dict(p) for p in TIM_PLANS]

    if nio_coverage:
        plans += [dict(p) for p in NIO_PLANS]

    return {
        "cep": cep_clean,
        "number": number,
        "address": address,
        "location": {
            "city": address["city"],
            "state": address["state"],
            "locationId": "",
        },
        "totalPlans": len(plans),
        "plans": plans,
        "nioCoverage": nio_coverage,
    }


def _build_coverage_string(
    has_claro: bool,
    claro_promo: bool,
    has_tim: bool,
    has_nio: bool,
) -> str:
    """Map provider presence flags to the exact automation offer labels."""
    claro_label = "Claro Promo" if claro_promo else "Claro"

    if has_claro and has_tim and has_nio:
        return f"Ofertas {claro_label} Tim Nio"
    if has_claro and has_tim:
        return f"Ofertas {claro_label} Tim" if claro_promo else "Ofertas Tim Claro"
    if has_claro and has_nio:
        return f"Ofertas {claro_label} Nio" if claro_promo else "Ofertas Claro Nio"
    if has_tim and has_nio:
        return "Ofertas Tim Nio"
    if has_claro:
        return f"Ofertas {claro_label}"
    if has_tim:
        return "Ofertas Tim"
    if has_nio:
        return "Ofertas Nio"
    return "Ofertas Sem cobertura"


def get_coverage_string(cep: str, number: str) -> str:
    """Return a human-readable coverage string for the given address."""
    cep_clean = _clean_cep(cep)
    if len(cep_clean) != 8:
        return "Sem cobertura"

    has_claro = cep_has_coverage(cep_clean, "ceps_claro")
    has_tim = cep_has_coverage(cep_clean, "ceps_tim")
    nio_coverage = cep_has_coverage(cep_clean, "ceps_nio")

    address = _fetch_address(cep_clean)
    city_normalized = _normalize(address["city"]) if address["city"] else ""

    claro_promo = has_claro and bool(city_normalized) and city_is_promo(city_normalized)

    return _build_coverage_string(has_claro, claro_promo, has_tim, nio_coverage)


if __name__ == "__main__":
    import sys
    cep = sys.argv[1] if len(sys.argv) > 1 else "15014050"
    num = sys.argv[2] if len(sys.argv) > 2 else "3495"
    result = search_plans(cep, num)
    print(json.dumps(result, indent=2, ensure_ascii=False))
