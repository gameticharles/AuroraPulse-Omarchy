"""Making the catalogue's own labels fit on a chip and readable.

Radio Browser hands over ISO country codes with the UN's official long-form
names attached, and tags that are whatever the station's owner typed. Neither
is wrong, but both make a filter list unusable: "The United Kingdom Of Great
Britain And Northern Ireland" next to "GB" in a dropdown, and a genre list
whose second entry is "fm".

The country names are fixed by a general rule plus a short list of the cases
the rule cannot win. The tag list is only ever used to *hide* obvious
non-genres, never to invent genres that are not there.
"""

# Tags that are not genres no matter how many stations carry them. Every entry
# is a word that describes a station rather than its music, and each one is
# common enough in the mirror to reach the top of a count-ranked list.
NOT_A_GENRE = frozenset((
    "fm", "am", "radio", "station", "stations", "hd", "live", "online",
    "internet", "stream", "streaming", "music radio", "digital", "dab",
    "webradio", "community", "local", "24/7", "24-7", "talk radio",
))

# Where the official long form is clumsy rather than merely long.
_COUNTRY_OVERRIDES = {
    "RU": "Russia", "KR": "South Korea", "KP": "North Korea",
    "VN": "Vietnam", "IR": "Iran", "TZ": "Tanzania", "VE": "Venezuela",
    "BO": "Bolivia", "MD": "Moldova", "SY": "Syria", "LA": "Laos",
    "BN": "Brunei", "CV": "Cape Verde", "CD": "DR Congo",
    "CI": "Côte d'Ivoire", "MK": "North Macedonia", "SZ": "Eswatini",
    "TL": "Timor-Leste", "PS": "Palestine", "VA": "Vatican City",
    "HK": "Hong Kong", "MO": "Macau", "TW": "Taiwan",
}


def country_name(code, raw=None):
    """A short, readable country name for a code.

    The general rule does most of the work: drop a leading article, and stop
    at " Of " so "The United States Of America" becomes "United States". The
    overrides are the cases where the official name is not just long but
    misleading in a list of countries.
    """
    code = (code or "").strip().upper()
    if code in _COUNTRY_OVERRIDES:
        return _COUNTRY_OVERRIDES[code]
    name = (raw or "").strip()
    if not name or name == code:
        return code
    if name.lower().startswith("the "):
        name = name[4:]
    # "Bolivia, Plurinational State Of" and friends.
    name = name.split(",")[0].strip()
    lowered = name.lower()
    for marker in (" of ",):
        index = lowered.find(marker)
        if index > 0:
            name = name[:index].strip()
            lowered = name.lower()
    if lowered in ("russian federation", "federation"):
        return "Russia"
    if lowered == "korea, republic of" or lowered == "republic of korea":
        return "South Korea"
    return name or code


# iptv-org publishes a bare two-letter code and no name at all, so the TV
# filter showed "UK", "DO", "CZ" and 138 more with nothing to read. These are
# the codes the mirror actually carries, in the order they matter by station
# count; anything absent falls back to the code, which is what it showed
# before, so an unknown code is never worse off.
COUNTRY_NAMES = {
    "US": "United States", "IN": "India", "RU": "Russia",
    "DE": "Germany", "SE": "Sweden", "ES": "Spain", "BR": "Brazil",
    "IT": "Italy", "UK": "United Kingdom", "DO": "Dominican Republic",
    "CL": "Chile", "TR": "Turkey", "FR": "France", "UA": "Ukraine",
    "AR": "Argentina", "NL": "Netherlands", "ID": "Indonesia",
    "PE": "Peru", "MX": "Mexico", "CA": "Canada", "CN": "China",
    "HU": "Hungary", "RO": "Romania", "PL": "Poland", "IR": "Iran",
    "CO": "Colombia", "PK": "Pakistan", "KR": "South Korea",
    "EC": "Ecuador", "HN": "Honduras", "VN": "Vietnam", "TH": "Thailand",
    "VE": "Venezuela", "BO": "Bolivia", "CZ": "Czechia",
    "GR": "Greece", "CR": "Costa Rica", "BG": "Bulgaria",
    "GT": "Guatemala", "PY": "Paraguay", "IE": "Ireland",
    "AT": "Austria", "CH": "Switzerland", "BE": "Belgium",
    "FI": "Finland", "NO": "Norway", "DK": "Denmark",
    "PT": "Portugal", "IL": "Israel", "SA": "Saudi Arabia",
    "AE": "UAE", "EG": "Egypt", "ZA": "South Africa", "NG": "Nigeria",
    "KE": "Kenya", "MA": "Morocco", "DZ": "Algeria", "TN": "Tunisia",
    "AU": "Australia", "NZ": "New Zealand", "JP": "Japan",
    "SG": "Singapore", "MY": "Malaysia",
    "PH": "Philippines", "BD": "Bangladesh", "LK": "Sri Lanka",
    "RS": "Serbia", "BA": "Bosnia", "MK": "North Macedonia",
    "AL": "Albania", "MD": "Moldova", "BY": "Belarus", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "SI": "Slovenia", "SK": "Slovakia",
    "HR": "Croatia", "GE": "Georgia", "AM": "Armenia", "AZ": "Azerbaijan",
    "UZ": "Uzbekistan", "KZ": "Kazakhstan", "JO": "Jordan",
    "LB": "Lebanon", "IQ": "Iraq", "SY": "Syria", "YE": "Yemen",
    "OM": "Oman", "QA": "Qatar", "KW": "Kuwait", "BH": "Bahrain",
    "AF": "Afghanistan", "NP": "Nepal", "MM": "Myanmar", "KH": "Cambodia",
    "LA": "Laos", "BN": "Brunei", "TW": "Taiwan", "HK": "Hong Kong",
    "CU": "Cuba", "JM": "Jamaica", "TT": "Trinidad", "UY": "Uruguay",
    "PR": "Puerto Rico", "PA": "Panama", "NI": "Nicaragua", "SV": "El Salvador",
    "BZ": "Belize", "HT": "Haiti", "GY": "Guyana", "SR": "Suriname", "FJ": "Fiji", "PG": "Papua New Guinea",
    "ZW": "Zimbabwe", "ZM": "Zambia", "UG": "Uganda", "GH": "Ghana",
    "TZ": "Tanzania", "ET": "Ethiopia", "SN": "Senegal", "CI": "Côte d'Ivoire",
    "CM": "Cameroon", "IS": "Iceland", "LU": "Luxembourg",
    "MT": "Malta", "CY": "Cyprus", "LI": "Liechtenstein", "MC": "Monaco",
}

# The codes people type are not always the codes the directory uses. iptv-org
# files the United Kingdom as "UK"; everyone else says "GB" or types the name.
COUNTRY_ALIASES = {
    "GB": "UK", "GBR": "UK", "UNITED KINGDOM": "UK", "UK": "UK",
    "EL": "GR", "GRC": "GR", "GREECE": "GR",
    "CZECH REPUBLIC": "CZ", "CZECHIA": "CZ",
    "SOUTH KOREA": "KR", "REPUBLIC OF KOREA": "KR", "KOREA": "KR",
    "RUSSIAN FEDERATION": "RU", "RUSSIA": "RU",
    "USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
    "VATICAN": "VA", "VATICAN CITY": "VA",
}


def country_label(code):
    """A readable name for a bare country code."""
    code = (code or "").strip().upper()
    return COUNTRY_NAMES.get(code) or code


# name -> code, so a label typed into the filter resolves to the code the
# mirror actually stores.
_NAME_TO_CODE = {name: code for code, name in COUNTRY_NAMES.items()}


def country_code(value):
    """Normalise whatever the user or the directory supplied to one code.

    A code stays a code. Looking the input up in COUNTRY_NAMES - which is
    keyed by code and holds names - turned "SE" into "Sweden" and then
    filtered for a country nobody stores.

    An unrecognised value is returned upper-cased, so an unknown code still
    filters rather than silently matching nothing.
    """
    text = (value or "").strip().upper()
    if not text:
        return ""
    if text in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[text]
    if text in COUNTRY_NAMES:
        return text
    return _NAME_TO_CODE.get(text, text)


def is_genre(tag):
    """Whether a tag is worth offering as a genre.

    Only ever used to drop a tag, so a false negative costs a genre nobody
    was going to click and a false positive just puts a non-genre back in the
    list - which is why this is a short stoplist and not a vocabulary check.
    """
    return bool(tag) and tag.strip().lower() not in NOT_A_GENRE
