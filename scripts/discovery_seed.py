"""The starter candidate list for the discovery sweep.

Names, and nothing else. Every entry here is a company someone believes exists
and hires engineers in Bangalore, Hyderabad or Pune — that belief is a *guess*,
and the pipeline in crawler/discover.py is what turns it into evidence or throws
it away. Nothing in this file decides that a company gets tracked. A name that
resolves to no board, or to a board with no job in the three cities, is logged
and dropped no matter how confidently it is spelled here.

So the bar for adding a name is low on purpose: it costs a handful of HTTP
requests and it is checked. The bar for adding a name you are not sure *exists*
is different — that costs the same requests and can never pay them back, so an
invented name is the one real mistake this file can make.

TWO SECTIONS, ONE DIFFERENCE
----------------------------
`PRODUCT_AND_SAAS` are companies whose careers page is plausibly one of the six
Tier-1 boards, which is what the sweep can actually resolve from a name.

`ENTERPRISE_AND_GCC` are the tech-park tenants and global capability centres —
Manyata, Bagmane, RMZ Ecoworld, Embassy TechVillage and GolfLinks, Cessna,
Prestige Tech Park, Ecospace, ITPB and Global Tech Village in Bangalore; Hitec
City and Gachibowli in Hyderabad; Hinjewadi, Kharadi and Magarpatta in Pune.
Almost none of them will resolve, and that is expected rather than a failure:
an enterprise this size is on Workday, Taleo or SuccessFactors, and a Workday
tenant is three independent unguessable strings (see the Workday note in
fetchers/resolve.py). Membership in this section changes exactly one thing —
an unresolved name from it is logged with a "try Workday" note, so the manual
add-by-URL queue writes itself.
"""

from __future__ import annotations

from pathlib import Path

# --- Section 1: product / SaaS, plausibly on a Tier-1 board ----------------
#
# Indian product companies, Indian SaaS, and global product companies with a
# real engineering centre in one of the three cities. The nine already in the
# companies table are deliberately absent; a name that is already tracked is
# skipped by the pipeline anyway, so listing it would only cost requests.
PRODUCT_AND_SAAS: tuple[str, ...] = (
    # Indian consumer internet
    "Swiggy", "Zomato", "Flipkart", "Myntra", "Ola", "Ola Electric",
    "Paytm", "PolicyBazaar", "Dream11", "Nykaa", "Lenskart", "Urban Company",
    "Rapido", "BigBasket", "Cars24", "Spinny", "Udaan", "ShareChat",
    "InMobi", "Zepto", "Zerodha", "Khatabook", "Delhivery", "Licious",
    "Ninjacart", "Zetwerk", "Shiprocket",
    # Indian SaaS and developer tooling
    "Freshworks", "Zoho", "Chargebee", "BrowserStack", "LambdaTest", "Hasura",
    "Juspay", "Setu", "Icertis", "Druva", "Netradyne", "Uniphore",
    "Gupshup", "Exotel", "MoEngage", "CleverTap", "Whatfix", "Mindtickle",
    "Darwinbox", "Zenoti", "Capillary Technologies", "Vymo", "LeadSquared",
    "HighRadius", "Locus", "FarEye", "Sprinto", "Zluri", "Amagi",
    "Kore.ai", "Yellow.ai", "Haptik", "Observe.ai", "Harness", "Innovaccer",
    "Sarvam AI", "Fractal Analytics", "Mu Sigma", "Tiger Analytics",
    "LatentView Analytics",
    # Indian fintech / insurtech / healthtech / edtech
    "Jupiter Money", "Epifi", "BharatPe", "Pine Labs", "Cashfree Payments",
    "M2P Fintech", "Perfios", "KreditBee", "Lendingkart", "Acko",
    "Go Digit", "Practo", "PharmEasy", "MediBuddy", "HealthifyMe",
    "Qure.ai", "Unacademy", "Vedantu", "upGrad", "Simplilearn",
    "Scaler", "Physics Wallah", "Emeritus", "Ather Energy",
    # Global product companies with a Bangalore / Hyderabad / Pune centre
    "Uber", "Stripe", "Atlassian", "Databricks", "Snowflake", "MongoDB",
    "Confluent", "HashiCorp", "Elastic", "GitLab", "New Relic", "Splunk",
    "Zscaler", "CrowdStrike", "Palo Alto Networks", "SentinelOne", "Rubrik",
    "Nutanix", "Cohesity", "Pure Storage", "NetApp", "Commvault",
    "Informatica", "Couchbase", "Okta", "DigitalOcean", "Zoom", "Sprinklr",
    "Wayfair", "Rippling", "Turing", "Expedia Group", "MakeMyTrip",
)

# --- Section 2: tech-park tenants and GCCs, expected to need Workday -------
#
# The seventeen names the brief supplied as confirmed tenants lead the list;
# the rest are the other occupants of the same parks. An unresolved name from
# THIS tuple is what earns the "try Workday" note in discovered_companies.
ENTERPRISE_AND_GCC: tuple[str, ...] = (
    # Supplied as confirmed tenants of the Bangalore/Hyderabad/Pune parks
    "IBM", "Cognizant", "Nokia", "Rolls-Royce", "Philips", "AXA",
    "Target", "NXP Semiconductors", "GLOBALFOUNDRIES", "Harman", "Cerner",
    "Larsen & Toubro", "Justdial", "Intel", "Cisco", "HP", "Goldman Sachs",
    # Indian IT services with campuses in the same parks
    "Infosys", "Wipro", "Tata Consultancy Services", "Tech Mahindra",
    "HCLTech", "LTIMindtree", "Mphasis", "Persistent Systems", "Coforge",
    "Birlasoft", "KPIT Technologies", "Cyient", "Tata Elxsi", "Zensar",
    "Hexaware", "Sonata Software", "Happiest Minds", "Quest Global",
    # Global consultancies and banks with Bangalore / Hyderabad / Pune GCCs
    "Accenture", "Capgemini", "Deloitte", "JPMorgan Chase", "Morgan Stanley",
    "Wells Fargo", "Barclays", "HSBC", "Standard Chartered", "Deutsche Bank",
    "American Express", "Visa", "Mastercard", "PayPal", "State Street",
    "Fidelity Investments", "Northern Trust", "Societe Generale", "NatWest",
    # Global product / semiconductor / industrial GCCs
    "Qualcomm", "AMD", "Micron Technology", "Texas Instruments",
    "Applied Materials", "Western Digital", "Broadcom", "Synopsys",
    "Siemens", "Bosch", "Continental", "Honeywell", "ABB",
    "Schneider Electric", "Emerson", "Cummins", "Eaton", "John Deere",
    "Caterpillar", "General Electric", "Medtronic", "Shell", "Maersk",
    "Lowe's", "Tesco", "Optum", "S&P Global", "Thomson Reuters", "Nielsen",
    "Ericsson", "Samsung", "NTT Data", "Fujitsu", "Hitachi",
)

# Order is stable and duplicates are dropped: a run's report is easier to read
# against the file when the two agree, and a name typed twice must not be
# probed twice.
SEED_CANDIDATES: tuple[str, ...] = tuple(
    dict.fromkeys(PRODUCT_AND_SAAS + ENTERPRISE_AND_GCC)
)

# Lowercased for membership tests; see is_enterprise.
_ENTERPRISE_LOOKUP = frozenset(name.strip().lower() for name in ENTERPRISE_AND_GCC)


def is_enterprise(name: str) -> bool:
    """Is this name in the GCC/enterprise section?

    The heuristic behind the "try Workday" note, and deliberately a membership
    test rather than a regex over the name. "Is this a big enterprise?" is not
    something a name can answer — Target and Harman look like startups and are
    not; Persistent Systems looks like a product company and is a services firm
    — so the answer is the list a person wrote, not a pattern.
    """
    return name.strip().lower() in _ENTERPRISE_LOOKUP


def load_seed_file(path: str | Path) -> list[str]:
    """Read extra candidate names from a text file: one per line.

    Blank lines and `#` comments are ignored, surrounding whitespace is
    stripped, and duplicates are dropped while keeping first-seen order. That is
    the whole format — the point of the flag is that a new list of names needs
    no code change, so it must not need a schema either.
    """
    text = Path(path).read_text(encoding="utf-8")
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return list(dict.fromkeys(names))
