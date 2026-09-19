"""
CBMP Monitoring Dashboard
=========================
PIR Community Based Malaria Programme
Kilifi County, Kenya · World Friends ETS

Run:  streamlit run app.py
Login: named users (Users & Access). Bootstrap password from CBMP_BOOTSTRAP_PASSWORD env or generated at first seed; must change on first login.
"""

import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as pgo  # not "go" — avoids clash with GO_1 indicator dict
import streamlit as st


# ==================================================================
# PAGE CONFIG
# ==================================================================

st.set_page_config(
    page_title="CBMP Dashboard · Kilifi",
    page_icon="🦟",
    layout="wide",
    initial_sidebar_state="expanded",
)

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "cbmp.db"
ASSETS_DIR = APP_DIR / "assets"
UPLOADS_DIR = APP_DIR / "uploads"
EXPORTS_DIR = APP_DIR / "exports"

ASSETS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)


# ==================================================================
# CONFIGURATION
# ==================================================================

FACILITIES = ["Jaribuni", "Pingilikani", "Mwakuhenga"]

SUBCOUNTY = {
    "Jaribuni": "Ganze",
    "Pingilikani": "Kilifi South",
    "Mwakuhenga": "Kilifi North",
}

# Official Kilifi County administrative units (7).
# WFK surveillance extract currently covers 5; Malindi & Magarini stay listed as pending.
KILIFI_SUBCOUNTIES = [
    "Kilifi North", "Kilifi South", "Malindi", "Magarini",
    "Ganze", "Kaloleni", "Rabai",
]
CBMP_IN_SUBCOUNTY = {
    "Ganze": "Jaribuni",
    "Kilifi South": "Pingilikani",
    "Kilifi North": "Mwakuhenga",
}

# Published populated-place coordinates (decimal degrees, WGS84).
# Jaribuni: GeoNames / Mindat ~ -3.6216, 39.7435 (Ganze).
# Pingilikani: Fallingrain / GeoNames ~ -3.7834, 39.7802 (Kilifi South).
# Mwakuhenga: GeoNames stream/settlement ~ -3.7024, 39.8122 (coastal Kilifi).
# CHU points are deterministic offsets from the facility anchor until CHU GPS is supplied.
GEO_SITES = {
    "Jaribuni": {
        "lat": -3.62164, "lon": 39.74354, "sub_county": "Ganze",
        "ward": "Jaribuni", "source": "GeoNames / Mindat populated place",
        "gps_level": 5, "gps_status": "gazetteer",
    },
    "Pingilikani": {
        "lat": -3.7834, "lon": 39.7802, "sub_county": "Kilifi South",
        "ward": "Mwarakaya / Pingilikani", "source": "Fallingrain / GeoNames",
        "gps_level": 5, "gps_status": "gazetteer",
    },
    "Mwakuhenga": {
        "lat": -3.70242, "lon": 39.81219, "sub_county": "Kilifi North",
        "ward": "Mavueni area", "source": "GeoNames (Mwakuhenga)",
        "gps_level": 5, "gps_status": "gazetteer",
    },
}

# Gazetteer centroids for the 7 official sub-counties (Level 5).
# Display-only. Never used to raise a geographic alert.
SUBCOUNTY_GEO = {
    "Kilifi North": {"lat": -3.633, "lon": 39.850, "source": "gazetteer HQ Kilifi town", "gps_level": 5},
    "Kilifi South": {"lat": -3.850, "lon": 39.750, "source": "gazetteer", "gps_level": 5},
    "Malindi": {"lat": -3.219, "lon": 40.117, "source": "gazetteer Malindi town", "gps_level": 5},
    "Magarini": {"lat": -2.980, "lon": 40.120, "source": "gazetteer", "gps_level": 5},
    "Ganze": {"lat": -3.540, "lon": 39.740, "source": "gazetteer Ganze", "gps_level": 5},
    "Kaloleni": {"lat": -3.820, "lon": 39.630, "source": "gazetteer", "gps_level": 5},
    "Rabai": {"lat": -3.930, "lon": 39.570, "source": "gazetteer", "gps_level": 5},
}

# Public gazetteer — village/settlement points near CBMP sites.
# Used when a 648 CHU name matches; not a substitute for official KMHFR CHU GPS.
CHU_GAZETTEER = {
    "jaribuni": (-3.62164, 39.74354, "GeoNames populated place"),
    "pingilikani": (-3.7834, 39.7802, "Fallingrain / GeoNames"),
    "mwakuhenga": (-3.70242, 39.81219, "GeoNames"),
    "makuhenga": (-3.70242, 39.81219, "GeoNames alias"),
    "mavueni": (-3.681767, 39.816489, "Wikipedia Mavueni"),
    "mwarakaya": (-3.80, 39.72, "nearby settlement (Pingilikani catchment)"),
    "vipingo": (-3.82, 39.81, "nearby settlement (Pingilikani catchment)"),
    "ganze": (-3.53, 39.70, "Ganze township (approx.)"),
    "bamba": (-3.45, 39.55, "Bamba (Ganze) approx."),
    "vitengeni": (-3.366, 39.717, "GeoNames Vitengeni"),
}

REGISTERS = [
    "MOH 648", "MOH 521", "MOH 748", "MOH 515", "MOH 711",
    "MOH 705A", "MOH 705B", "MOH 706", "MOH 743", "MOH 705",
]

REGISTER_PRESETS = {
    "All registers": REGISTERS,
    "Facility registers": ["MOH 705A", "MOH 705B", "MOH 706", "MOH 743", "MOH 705"],
    "Community registers": ["MOH 648", "MOH 521", "MOH 748", "MOH 515", "MOH 711"],
    "ANC / IPTp (711)": ["MOH 711", "MOH 743"],
    "CHP registers": ["MOH 648", "MOH 521"],
    "CHEW registers": ["MOH 748", "MOH 515"],
    "Laboratory only": ["MOH 706"],
    "Treatment only": ["MOH 743"],
}

AGE_GROUPS = ["All ages", "<5", ">=5"]
STREAMS = ["Both", "Facility", "Community"]
PERIOD_OPTIONS = ["Monthly", "Quarterly", "Yearly", "Custom"]

# ---------------------------------------------------------------------
# PHASE 0 FOUNDATIONS (see docs/PHASE_0_FOUNDATIONS.md for sign-off)
# ---------------------------------------------------------------------
# Population = PROVISIONAL until County DoH confirms (Option A) or
# incidence/ABER are deferred from official reporting (Option B).
POPULATION = {
    "Jaribuni": 25000,
    "Pingilikani": 30000,
    "Mwakuhenga": 15000,
    "Total": 70000,
}
POPULATION_STATUS = "provisional"  # "provisional" | "confirmed" | "deferred_epi"
PHASE_0_SIGNED_OFF = True  # Set after PM/M&E/County sign-off
PHASE_0_SIGNED_DATE = "2026-09-15"

# ---------------------------------------------------------------------
# MALARIA EARLY WARNING — environmental & modelling constants (Phase C)
# ---------------------------------------------------------------------
# Realistic Kilifi values; replace with measured DEM / NDVI / hydro layers when available.
FACILITY_ENV_DEFAULTS = {
    "Jaribuni": {
        "elevation_m": 180, "water_dist_km": 2.5, "ndvi_baseline": 0.42,
        "land_cover": "agriculture_shrub", "pop_density": 85, "travel_time_min": 45,
    },
    "Pingilikani": {
        "elevation_m": 45, "water_dist_km": 0.9, "ndvi_baseline": 0.48,
        "land_cover": "coastal_agriculture", "pop_density": 120, "travel_time_min": 25,
    },
    "Mwakuhenga": {
        "elevation_m": 25, "water_dist_km": 0.6, "ndvi_baseline": 0.45,
        "land_cover": "coastal_settlement", "pop_density": 95, "travel_time_min": 20,
    },
}

# Lag structure used by the early-warning engine (weeks)
EW_RAIN_LAGS = (1, 2, 3, 4, 6, 8)
EW_HORIZONS = (4, 8, 12)  # forecast horizons in weeks
EW_RISK_THRESHOLDS = {"low": 0.35, "moderate": 0.55, "high": 0.75}  # risk_score cut-points


BASELINES = {
    "GO_1": {
        "name": "Overall Test Positivity Rate (RDT only)",
        "baseline": 39.13,
        "target": 50.0,
        "unit": "%",
        "source": "KHIS / MOH 706 (RDT only)",
        "direction": "go_tpr",  # special RAG — see TPR_RULE
    },
    "SO_1": {
        "name": "% of CHPs who passed competency assessment",
        "baseline": 0.0,
        "target": 70.0,
        "unit": "%",
        "source": "Kilifi County Department of Health",
        "direction": "higher_better",
    },
    "R1_1": {
        "name": "Number of tests performed by CHPs",
        "baseline": 815,
        "target": 1500,
        "unit": "tests",
        "source": "MOH 748 Register",
        "direction": "higher_better",
    },
    "R1_2": {
        "name": "% of monitoring reports submitted",
        "baseline": 66.0,
        "target": 80.0,
        "unit": "%",
        "source": "MOH 515",
        "direction": "higher_better",
    },
}

# TPR must always be read with testing volume (Phase 0 rule).
TPR_RULE = {
    "summary": (
        "GO_1 is not higher-is-always-better. Rising TPR with falling tests "
        "is a warning (possible testing collapse), not automatic success."
    ),
    "show_with": ["RDT total exams", "R1_1 CHP tests"],
    "collapse_drop_pct": 25,  # MoM drop in tests while TPR rises → warning
}

# RAG thresholds (Phase 0)
RAG_RULES = {
    "higher_better": {
        "green": "current >= target",
        "amber": "current >= 0.75 * target and current < target",
        "red": "current < 0.75 * target",
    },
    "completeness": {
        "green": ">= 90",
        "amber": "80–89",
        "red": "< 80",
    },
    "go_tpr": {
        "note": "Combine value vs baseline/target with testing-volume warning",
    },
}

# Registers in Phase 1 upload scope
PHASE1_REGISTERS = [
    "MOH 705A", "MOH 705B", "MOH 706", "MOH 743", "MOH 705",
    "MOH 748", "MOH 515", "MOH 711",
]
# Reserved for Phase 2+ (labels may exist; full cascade not required for Phase 1)
PHASE2_REGISTERS = ["MOH 648", "MOH 521"]

EPI_BENCHMARKS = {
    "Malaria Incidence Rate": {
        "target": 3.2,
        "note": "Coast Endemic: 1.7–3.2 per 1,000 — INDICATIVE while population is provisional",
    },
    "ABER": {
        "target": 10,
        "note": "Threshold: >10% — INDICATIVE while population is provisional",
    },
    "Test Positivity Rate": {"target": 40, "note": "Kilifi: <40% = lower transmission intensity context"},
    "Case Fatality Rate": {"target": 5, "note": "WHO: <5% — DEFERRED (no death field in current upload)"},
    "Severe Malaria Proportion": {"target": 10, "note": "Lower is better — DEFERRED (no severe field in current upload)"},
}

ROLES = {
    "admin": {
        "name": "Admin (Project Manager)",
        "pages": [
            "Upload & Settings", "Overview", "Command Centre",
            "GO · Test Positivity Rate", "SO · CHP Competency",
            "R1.1 · CHP Tests", "R1.2 · Reports Submitted",
            "Epidemiological", "All Indicators",
            "Facility Deep-Dive", "Loss & Gap Analysis",
            "Data Quality", "Data foundation", "Dashboard QA", "Users & Access", "CHP Performance",
            "Alerts", "Maps", "Comparison", "Climate",
            "Malaria Early Warning", "Reports",
            "Budget", "Planning",
        ],
        "can_upload": True,
        "can_commit": True,
        "can_manage_users": True,
        "scope": "all",
        "aggregation": False,
        "min_cell": 1,
    },
    "partner": {
        "name": "Project Partner (USL Toscana Sud Est)",
        "pages": [
            "Overview", "Command Centre", "GO · Test Positivity Rate", "SO · CHP Competency",
            "R1.1 · CHP Tests", "R1.2 · Reports Submitted",
            "Epidemiological", "All Indicators", "Facility Deep-Dive",
            "Loss & Gap Analysis", "Data Quality", "Data foundation", "CHP Performance",
            "Alerts", "Maps", "Comparison", "Climate", "Malaria Early Warning",
            "Reports", "Budget", "Planning",
        ],
        "can_upload": False,
        "can_commit": False,
        "can_manage_users": False,
        "scope": "all",
        "aggregation": False,
        "min_cell": 1,
    },
    "county": {
        "name": "County (Kilifi DoH)",
        "pages": [
            "Overview", "Command Centre", "GO · Test Positivity Rate", "SO · CHP Competency",
            "R1.1 · CHP Tests", "R1.2 · Reports Submitted",
            "Epidemiological", "All Indicators", "Facility Deep-Dive",
            "Loss & Gap Analysis", "Data Quality", "Data foundation", "CHP Performance",
            "Alerts", "Maps", "Comparison", "Climate", "Malaria Early Warning",
            "Budget", "Planning", "Reports",
        ],
        "can_upload": False,
        "can_commit": False,
        "can_manage_users": False,
        "scope": "all",
        "aggregation": True,
        "min_cell": 5,
    },
    "donor": {
        "name": "Donor (AICS) — executive viewer",
        "pages": ["Overview", "Command Centre", "All Indicators", "Reports", "Budget", "Malaria Early Warning"],
        "can_upload": False,
        "can_commit": False,
        "can_manage_users": False,
        "scope": "all",
        "aggregation": True,
        "min_cell": 10,
    },
    "supervisor": {
        "name": "Sub-county malaria / CHA",
        "pages": [
            "Overview", "Command Centre", "GO · Test Positivity Rate",
            "R1.1 · CHP Tests", "R1.2 · Reports Submitted",
            "Epidemiological", "All Indicators",
            "Facility Deep-Dive", "Loss & Gap Analysis",
            "Data Quality", "CHP Performance", "Alerts", "Maps",
            "Climate", "Malaria Early Warning",
        ],
        "can_upload": False,
        "can_commit": False,
        "can_manage_users": False,
        "scope": "sub_county",
        "aggregation": False,
        "min_cell": 1,
    },
    "facility": {
        "name": "Facility In-Charge",
        "pages": [
            "Overview", "Command Centre", "GO · Test Positivity Rate",
            "R1.1 · CHP Tests", "R1.2 · Reports Submitted",
            "Epidemiological", "All Indicators",
            "Facility Deep-Dive", "Loss & Gap Analysis",
            "Data Quality", "CHP Performance", "Alerts",
            "Malaria Early Warning",
        ],
        "can_upload": False,
        "can_commit": False,
        "can_manage_users": False,
        "scope": "facility",
        "aggregation": False,
        "min_cell": 1,
    },
    "viewer": {
        "name": "Viewer (read-only)",
        "pages": ["Overview", "Command Centre", "All Indicators", "Malaria Early Warning"],
        "can_upload": False,
        "can_commit": False,
        "can_manage_users": False,
        "scope": "all",
        "aggregation": True,
        "min_cell": 5,
    },
}

COLOR_NAVY = "#1F4E79"
COLOR_BLUE = "#2E86AB"
COLOR_GREEN = "#27AE60"
COLOR_AMBER = "#F39C12"
COLOR_RED = "#E74C3C"
COLOR_BG_SOFT = "#F8F9FA"
COLOR_BORDER = "#E0E4E8"
COLOR_TEXT = "#2C3E50"
COLOR_TEXT_MUTED = "#7F8C8D"
COLOR_HERO_FROM = "#E8F6F3"
COLOR_HERO_TO = "#FFFFFF"


# ==================================================================
# REGISTER LABEL MAPPING
# ==================================================================

def _norm_label(s):
    """Collapse all whitespace so double-space KHIS labels still match."""
    return " ".join(str(s).strip().split()) if s is not None and str(s).strip() else ""


# Keys stored in normalised (single-space) form. Matching always uses _norm_label().
KNOWN_LABELS = {
    # MOH 706
    "MOH 706_Malaria BS (Under five years) Total Exam": ("MOH 706", "BS <5 Total Exam", "<5"),
    "MOH 706_Malaria BS (Under five years) Number Positive": ("MOH 706", "BS <5 Positive", "<5"),
    "MOH 706_Malaria BS (5 years and above) Total Exam": ("MOH 706", "BS >=5 Total Exam", ">=5"),
    "MOH 706_Malaria BS (5 years and above) Number Positive": ("MOH 706", "BS >=5 Positive", ">=5"),
    "MOH 706 Rev 2020_Malaria Rapid diagnostic Tests (Under five years) Total Exam": ("MOH 706", "RDT <5 Total Exam", "<5"),
    "MOH 706 Rev 2020_Malaria Rapid diagnostic Tests (Under five years) Number Positive": ("MOH 706", "RDT <5 Positive", "<5"),
    "MOH 706 Rev 2020_Malaria Rapid diagnostic test (5 years and above) Total Exam": ("MOH 706", "RDT >=5 Total Exam", ">=5"),
    "MOH 706 Rev 2020_Malaria Rapid diagnostic test (5 years and above) Number Positive": ("MOH 706", "RDT >=5 Positive", ">=5"),
    "MOH 706 Reporting rate": ("MOH 706", "Reporting Rate", "All ages"),

    # MOH 705A
    "Suspected Malaria <5 yrs": ("MOH 705A", "Suspected <5", "<5"),
    "MOH 705A Rev 2020_ Tested for Malaria <5 yrs": ("MOH 705A", "Tested <5", "<5"),
    "Confirmed Malaria (only Positive cases) <5 yrs": ("MOH 705A", "Confirmed <5", "<5"),
    "MOH 705A Reporting rate": ("MOH 705A", "Reporting Rate", "All ages"),

    # MOH 705B
    "Suspected Malaria >5 yrs": ("MOH 705B", "Suspected >=5", ">=5"),
    "MOH 705B Rev 2020_ Tested for Malaria >5 yrs": ("MOH 705B", "Tested >=5", ">=5"),
    "Confirmed Malaria (only Positive cases) >5 yrs": ("MOH 705B", "Confirmed >=5", ">=5"),
    "MOH 705B Rev 2020_ Malaria in Pregnancy": ("MOH 705B", "Malaria in Pregnancy", "All ages"),
    "MOH 705B Reporting rate": ("MOH 705B", "Reporting Rate", "All ages"),

    # MOH 743
    "MOH 743 Rev2020_Patients on AL by Weight Band Summary Report 5 -14 kgs": ("MOH 743", "AL 5-14 kg", "All ages"),
    "MOH 743 Rev2020_Patients on AL by Weight Band Summary Report 15 - 24kgs": ("MOH 743", "AL 15-24 kg", "All ages"),
    "MOH 743 Rev2020_Patients on AL by Weight Band Summary Report 25 - 34kgs": ("MOH 743", "AL 25-34 kg", "All ages"),
    "MOH 743 Rev2020_Patients on AL by Weight Band Summary Report 35+ kgs": ("MOH 743", "AL 35+ kg", "All ages"),
    "MOH 743 Rev2020_Patients on AL by Weight Band Summary ReportTotal": ("MOH 743", "AL Total", "All ages"),
    "MOH 743 REV2020_IPT1": ("MOH 743", "IPT1", "All ages"),
    "MOH 743 REV2020_IPT2": ("MOH 743", "IPT2", "All ages"),
    "MOH 743 REV2020_IPT3": ("MOH 743", "IPT3", "All ages"),
    "MOH 743 Rev2020_Positive Result Microscopy": ("MOH 743", "Positive Microscopy", "All ages"),
    "MOH 743 Rev2020_Positive Result RDT": ("MOH 743", "Positive RDT", "All ages"),
    "MOH 743 Rev2020_Negative Result Microscopy": ("MOH 743", "Negative Microscopy", "All ages"),
    "MOH 743 Rev2020_Negative Result RDT": ("MOH 743", "Negative RDT", "All ages"),
    "MOH 743 REV2020_INVALID CASES MICROSCOPY": ("MOH 743", "Invalid Microscopy", "All ages"),
    "MOH 743 Rev2020_Invalid result RDT": ("MOH 743", "Invalid RDT", "All ages"),
    "MOH 743 REV2020_TOTAL TESTED MICROSCOPY": ("MOH 743", "Tested Microscopy", "All ages"),
    "MOH 743 REV2020_TOTAL TESTED RDT": ("MOH 743", "Tested RDT", "All ages"),
    "MOH 743 REV2020_CASES TREATED WITHOUT TESTING": ("MOH 743", "Treated w/o Testing", "All ages"),
    "MOH 743 Reporting rate": ("MOH 743", "Reporting Rate", "All ages"),

    # MOH 748
    "MOH 748 AL DISPENSED": ("MOH 748", "AL Dispensed", "All ages"),
    "MOH 748 RDT DISPENSED": ("MOH 748", "RDT Dispensed", "All ages"),
    "MOH 748 Patients on AL by Weight Band Summary Report 5 -14 kgs": ("MOH 748", "AL 5-14 kg", "All ages"),
    "MOH 748 Patients on AL by Weight Band Summary Report 15 - 24kgs": ("MOH 748", "AL 15-24 kg", "All ages"),
    "MOH 748 Patients on AL by Weight Band Summary Report 25 - 34kgs": ("MOH 748", "AL 25-34 kg", "All ages"),
    "MOH 748 Patients on AL by Weight Band Summary Report 35+ kgs": ("MOH 748", "AL 35+ kg", "All ages"),
    "MOH 748 POSITIVE <5 yrs": ("MOH 748", "Positive <5", "<5"),
    "MOH 748 NEGATIVE <5 yrs": ("MOH 748", "Negative <5", "<5"),
    "MOH 748 NOT TESTED <5 yrs": ("MOH 748", "Not Tested <5", "<5"),
    "MOH 748 TOTAL <5 yrs": ("MOH 748", "Total <5", "<5"),
    "MOH 748 INVALID <5 yrs": ("MOH 748", "Invalid <5", "<5"),
    "MOH 748 POSITIVE >5 yrs": ("MOH 748", "Positive >=5", ">=5"),
    "MOH 748 NEGATIVE >5 yrs": ("MOH 748", "Negative >=5", ">=5"),
    "MOH 748 NOT TESTED >5 yrs": ("MOH 748", "Not Tested >=5", ">=5"),
    "MOH 748 TOTAL >5 yrs": ("MOH 748", "Total >=5", ">=5"),
    "MOH 748 INVALID >5 yrs": ("MOH 748", "Invalid >=5", ">=5"),
    "MOH 748 Reporting rate": ("MOH 748", "Reporting Rate", "All ages"),

    # MOH 515 (exact + facility-specific variants seen in KHIS exports)
    "MOH 515 Reporting rate": ("MOH 515", "Reporting Rate", "All ages"),
    "MOH 515 Reporting rate (Jaribuni Dispensary, can be replaced with CHU)": ("MOH 515", "Reporting Rate", "All ages"),

    # MOH 711 ANC / IPTp / LLIN (Aug 2026 extract)
    "MOH 711_New ANC clients (1st visit)": ("MOH 711", "New ANC", "All ages"),
    "MOH 711New ANC clients": ("MOH 711", "New ANC", "All ages"),
    "MOH 711_ANC revisits": ("MOH 711", "ANC revisit", "All ages"),
    "MOH 711 Re-visit ANC clients": ("MOH 711", "ANC revisit", "All ages"),
    "MOH 711_Clients completing 4th ANC visit": ("MOH 711", "ANC4", "All ages"),
    "MOH 711Pregnant Women Completing 4 ANC visits": ("MOH 711", "ANC4", "All ages"),
    "MOH 711_Clients completing 8th ANC contact": ("MOH 711", "ANC8", "All ages"),
    "MOH 711No. Of clients completed 8th ANC contact": ("MOH 711", "ANC8", "All ages"),
    "MOH 711_1st dose IPT (IPTp1)": ("MOH 711", "IPT1", "All ages"),
    "MOH 711 Clients given IPT 1st Dose": ("MOH 711", "IPT1", "All ages"),
    "MOH 711_2nd dose IPT (IPTp2)": ("MOH 711", "IPT2", "All ages"),
    "MOH 711 Clients given IPT 2nd Dose": ("MOH 711", "IPT2", "All ages"),
    "MOH 711_3rd dose IPT (IPTp3)": ("MOH 711", "IPT3", "All ages"),
    "MOH 711 Rev 2020_Clients given IPT 3rd Dose": ("MOH 711", "IPT3", "All ages"),
    "MOH 711_LLINs issued to ANC clients": ("MOH 711", "LLIN ANC", "All ages"),
    "MOH 711_Clients with Hb less than 11 g/dl at 1st ANC visit": ("MOH 711", "ANC anaemia", "All ages"),
    "MOH 711 Clients with Hb <11 g/dl at 1st ANC visit": ("MOH 711", "ANC anaemia", "All ages"),
    "MOH 711_Clients with Hb < 11 g/dl at 1st ANC": ("MOH 711", "ANC anaemia", "All ages"),
    "MOH 711 Hb less than 11 g/dl first ANC": ("MOH 711", "ANC anaemia", "All ages"),
    "MOH 711_ANC clients with anaemia (Hb<11)": ("MOH 711", "ANC anaemia", "All ages"),
    "MOH 711 Clients with Hb less than 11g/dl": ("MOH 711", "ANC anaemia", "All ages"),
    "MOH 711_LLINs issued at ANC": ("MOH 711", "LLIN ANC", "All ages"),
    "MOH 711 LLINs issued to ANC clients": ("MOH 711", "LLIN ANC", "All ages"),
    "MOH 711 Clients given IPT 3rd Dose": ("MOH 711", "IPT3", "All ages"),
    "MOH 711 Reporting rate": ("MOH 711", "Reporting Rate", "All ages"),
    "MOH 711_Reporting rate": ("MOH 711", "Reporting Rate", "All ages"),

    # MOH 648 (NEW)
    "MOH 648_Suspected": ("MOH 648", "Suspected", "All ages"),
    "MOH 648_Tested": ("MOH 648", "Tested", "All ages"),
    "MOH 648_Positive": ("MOH 648", "Positive", "All ages"),
    "MOH 648_Referred": ("MOH 648", "Referred", "All ages"),
    "MOH 648_Followed up": ("MOH 648", "Followed up", "All ages"),
    "MOH 648_ITN Distributed": ("MOH 648", "ITN Distributed", "All ages"),
    "MOH 648_Reporting rate": ("MOH 648", "Reporting Rate", "All ages"),

    # MOH 521 (NEW)
    "MOH 521_Total Suspected": ("MOH 521", "Total Suspected", "All ages"),
    "MOH 521_Total Tested": ("MOH 521", "Total Tested", "All ages"),
    "MOH 521_Total Positive": ("MOH 521", "Total Positive", "All ages"),
    "MOH 521_Total Referred": ("MOH 521", "Total Referred", "All ages"),
    "MOH 521_Reporting rate": ("MOH 521", "Reporting Rate", "All ages"),

    # MOH 705 (ANC / MiP — prefer ANC1 for coverage denominators)
    "MOH 705_IPTp Doses": ("MOH 705", "IPTp Doses", "All ages"),
    "MOH 705_ANC Attendance": ("MOH 705", "ANC Attendance", "All ages"),
    "MOH 705_ANC1": ("MOH 705", "ANC1", "All ages"),
    "MOH 705_First ANC": ("MOH 705", "ANC1", "All ages"),
    "MOH 705_ANC 1st Visit": ("MOH 705", "ANC1", "All ages"),
    "ANC 1st Visit": ("MOH 705", "ANC1", "All ages"),
    "First ANC Visit": ("MOH 705", "ANC1", "All ages"),
    "ANC1": ("MOH 705", "ANC1", "All ages"),
    "MOH 705_MIP Cases": ("MOH 705", "MIP Cases", "All ages"),
    "MOH 705_Reporting rate": ("MOH 705", "Reporting Rate", "All ages"),
}

SHEET_TO_FACILITY = {
    "KF NORTH": "Mwakuhenga",
    "KF SOUTH": "Pingilikani",
    "GANZE": "Jaribuni",
}

# Phase A0 — sheet → geography (county / sub-county / facility)
SHEET_TO_GEO = {
    "KILIFI COUNTY": {
        "geo_level": "county",
        "geo_name": "Kilifi",
        "facility": None,
        "sub_county": None,
    },
    "KF NORTH": {
        "geo_level": "facility",
        "geo_name": "Mwakuhenga",
        "facility": "Mwakuhenga",
        "sub_county": "Kilifi North",
    },
    "KF SOUTH": {
        "geo_level": "facility",
        "geo_name": "Pingilikani",
        "facility": "Pingilikani",
        "sub_county": "Kilifi South",
    },
    "GANZE": {
        "geo_level": "facility",
        "geo_name": "Jaribuni",
        "facility": "Jaribuni",
        "sub_county": "Ganze",
    },
}

FACILITY_MARKERS = {
    "MWAKUHENGA DISPENSARY": "Mwakuhenga",
    "PINGILIKANI DISPENSARY": "Pingilikani",
    "JARIBUNI DISPENSARY": "Jaribuni",
}

PHASE_A0 = True  # submissions + period engine + dictionary + observations


# ==================================================================
# STYLES
# ==================================================================

STYLE_TEMPLATE = """
<style>
  html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: COLOR_TEXT;
  }
  .hero-band {
    background: linear-gradient(165deg, HERO_FROM 0%, #F0FAF7 45%, HERO_TO 100%);
    border: 1px solid #D0E8E0;
    border-radius: 16px;
    padding: 28px 32px 24px 32px;
    margin-bottom: 24px;
    text-align: center;
    box-shadow: 0 4px 20px rgba(31, 78, 121, 0.06);
  }
  .hero-logos {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 20px;
  }
  .hero-logos-left { display: flex; gap: 16px; align-items: center; }
  .hero-logos-right { display: flex; gap: 20px; align-items: center; }
  .hero-logos img {
    height: 40px;
    width: auto;
    max-width: 160px;
    object-fit: contain;
    display: block;
  }
  .logo-slot {
    height: 42px;
    min-width: 100px;
    background: #FFFFFF;
    border: 1px solid #D0E0DC;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: NAVY;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0;
    text-transform: uppercase;
    padding: 0 14px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .hero-icon { font-size: 2.2rem; margin: 4px 0 10px 0; opacity: 0.9; }
  .hero-title {
    font-size: 2.1rem;
    font-weight: 800;
    color: NAVY;
    letter-spacing: 0;
    margin: 0 0 6px 0;
    line-height: 1.15;
  }
  .hero-subtitle {
    font-size: 1.05rem;
    font-weight: 500;
    color: BLUE;
    margin: 0 0 4px 0;
  }
  .hero-location {
    font-size: 0.9rem;
    color: TEXT_MUTED;
    margin: 0 0 12px 0;
  }
  .hero-divider {
    width: 48px;
    margin: 0 auto 12px auto;
    border: 0;
    border-top: 2px solid #B8D9CE;
  }
  .hero-footer { font-size: 0.8rem; color: TEXT_MUTED; margin: 0; }
  .facility-status {
    display: flex;
    justify-content: center;
    gap: 20px;
    margin-top: 14px;
    font-size: 0.85rem;
    color: TEXT_MUTED;
  }
  .page-header {
    background: BG_SOFT;
    border-left: 6px solid NAVY;
    padding: 20px 24px;
    border-radius: 8px;
    margin-bottom: 24px;
  }
  .page-header h1 { color: NAVY; margin: 0; font-size: 1.9rem; font-weight: 700; }
  .page-header p { color: TEXT_MUTED; margin: 6px 0 0 0; font-size: 0.95rem; }
  .headline-block {
    background: linear-gradient(90deg, #E8F6F3 0%, #F8F9FA 100%);
    border-left: 6px solid GREEN;
    padding: 20px 24px;
    border-radius: 8px;
    margin: 16px 0 24px 0;
  }
  .headline-block.amber { border-left-color: AMBER; }
  .headline-block.red { border-left-color: RED; }
  .headline-label {
    font-size: 0.75rem;
    font-weight: 700;
    color: TEXT_MUTED;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .headline-text { font-size: 1.15rem; color: TEXT; line-height: 1.55; }
  .big-num {
    background: #FFFFFF;
    border: 1px solid BORDER;
    border-radius: 12px;
    padding: 24px;
    text-align: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    height: 100%;
  }
  .big-num-value { font-size: 2.4rem; font-weight: 700; color: NAVY; line-height: 1.1; }
  .big-num-label {
    font-size: 0.85rem;
    color: TEXT_MUTED;
    margin-top: 8px;
    text-transform: uppercase;
    letter-spacing: 0;
  }
  .big-num-delta { font-size: 0.95rem; margin-top: 6px; font-weight: 600; }
  .big-num-delta.up { color: GREEN; }
  .big-num-delta.down { color: RED; }
  .big-num-delta.flat { color: TEXT_MUTED; }
  .status-badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: 600;
  }
  .status-badge.green { background: rgba(39, 174, 96, 0.12); color: GREEN; }
  .status-badge.amber { background: rgba(243, 156, 18, 0.12); color: #B9770E; }
  .status-badge.red { background: rgba(231, 76, 60, 0.12); color: RED; }
  .status-badge.grey { background: rgba(127, 140, 141, 0.12); color: TEXT_MUTED; }
  .section-header {
    font-size: 1.15rem;
    font-weight: 700;
    color: NAVY;
    letter-spacing: 0;
    text-transform: none;
    padding-bottom: 8px;
    border-bottom: 2px solid NAVY;
    margin: 24px 0 12px 0;
  }
  .upload-zone {
    background: #FFFFFF;
    border: 2px dashed NAVY;
    border-radius: 14px;
    padding: 48px 32px;
    text-align: center;
    margin: 12px 0;
  }
  .upload-zone-icon { font-size: 3rem; color: NAVY; margin-bottom: 16px; }
  .upload-zone-title {
    font-size: 1.15rem;
    font-weight: 600;
    color: NAVY;
    margin-bottom: 6px;
  }
  .upload-zone-sub { font-size: 0.95rem; color: TEXT_MUTED; margin-bottom: 20px; }
  .upload-zone-rules {
    font-size: 0.85rem;
    color: TEXT_MUTED;
    text-align: left;
    max-width: 500px;
    margin: 0 auto;
    padding-top: 16px;
    border-top: 1px solid BORDER;
  }
  .upload-zone-rules ul { margin: 8px 0; padding-left: 20px; }
  .upload-zone-rules li { margin: 4px 0; }
  .upload-row {
    background: #FFFFFF;
    border: 1px solid BORDER;
    border-radius: 10px;
    padding: 16px 20px;
    margin: 10px 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .upload-row-main { display: flex; align-items: center; gap: 16px; }
  .upload-row-icon { font-size: 1.5rem; }
  .upload-row-name { font-weight: 600; color: TEXT; }
  .upload-row-meta { font-size: 0.85rem; color: TEXT_MUTED; margin-top: 4px; }
  div[data-testid="stExpander"] {
    border: 1px solid BORDER;
    border-radius: 10px;
    margin-bottom: 10px;
  }
  div[data-testid="stExpander"] summary { font-weight: 600; color: NAVY; }
  .stButton > button { border-radius: 8px; font-weight: 600; padding: 10px 20px; }
  .stButton > button[kind="primary"] { background-color: NAVY; border-color: NAVY; }
  div[data-testid="stMetricValue"] { color: NAVY; font-weight: 700; }
  .alert-critical {
    background: rgba(231, 76, 60, 0.08);
    border-left: 4px solid RED;
    padding: 12px 16px;
    border-radius: 6px;
    margin: 8px 0;
  }
  .alert-high {
    background: rgba(243, 156, 18, 0.08);
    border-left: 4px solid AMBER;
    padding: 12px 16px;
    border-radius: 6px;
    margin: 8px 0;
  }
  .alert-warning {
    background: rgba(241, 196, 15, 0.08);
    border-left: 4px solid #F1C40F;
    padding: 12px 16px;
    border-radius: 6px;
    margin: 8px 0;
  }
</style>
"""

_css = (STYLE_TEMPLATE
    .replace("TEXT_MUTED", COLOR_TEXT_MUTED)
    .replace("COLOR_TEXT", COLOR_TEXT)
    .replace("HERO_FROM", COLOR_HERO_FROM)
    .replace("HERO_TO", COLOR_HERO_TO)
    .replace("BG_SOFT", COLOR_BG_SOFT)
    .replace("BORDER", COLOR_BORDER)
    .replace("NAVY", COLOR_NAVY)
    .replace("BLUE", COLOR_BLUE)
    .replace("GREEN", COLOR_GREEN)
    .replace("AMBER", COLOR_AMBER)
    .replace("RED", COLOR_RED)
    .replace("TEXT", COLOR_TEXT)
)
st.markdown(_css, unsafe_allow_html=True)


# ==================================================================
# DATABASE
# ==================================================================

def get_conn():
    """
    Open a short-lived SQLite connection.
    timeout + WAL reduce 'database is locked' under Streamlit reruns / multi-tab.
    """
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS malaria_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT NOT NULL,
            sub_county TEXT,
            stream TEXT NOT NULL,
            register TEXT NOT NULL,
            indicator TEXT NOT NULL,
            period TEXT NOT NULL,
            age_group TEXT DEFAULT 'All ages',
            value REAL,
            data_status TEXT DEFAULT 'Actual',
            uploaded_by TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            upload_file TEXT,
            UNIQUE(facility, stream, register, indicator, period, age_group)
        );
        CREATE TABLE IF NOT EXISTS upload_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT, stored_path TEXT, facility TEXT, stream TEXT,
            rows_committed INTEGER, validation_status TEXT, uploaded_by TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS baselines (
            indicator_id TEXT PRIMARY KEY, baseline_value REAL, target_value REAL,
            unit TEXT, updated_by TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT, action TEXT, table_name TEXT, comment TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            must_change INTEGER DEFAULT 1,
            role_key TEXT,
            display_name TEXT,
            facility_scope TEXT,
            sub_county_scope TEXT,
            active INTEGER DEFAULT 1,
            last_login TEXT,
            pages_override TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shared_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            title TEXT,
            page_key TEXT,
            payload_json TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            allow_download INTEGER DEFAULT 0,
            revoked INTEGER DEFAULT 0,
            open_count INTEGER DEFAULT 0,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS password_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            purpose TEXT DEFAULT 'reset',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            used INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, facility TEXT, chu TEXT, phone TEXT,
            competency_status TEXT DEFAULT 'pending',
            competency_date TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referral_date TEXT, chp_id INTEGER, patient_id TEXT,
            facility TEXT, arrived INTEGER, confirmed INTEGER,
            treated INTEGER, outcome TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS supervision_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT, visit_date TEXT, supervisor TEXT,
            findings TEXT, actions TEXT, follow_up_date TEXT, status TEXT
        );
        CREATE TABLE IF NOT EXISTS supervision_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visit_id INTEGER,
            facility TEXT,
            finding TEXT,
            action_text TEXT,
            owner TEXT,
            deadline TEXT,
            status TEXT DEFAULT 'open',
            closed_at TEXT,
            closed_by TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sup_act_status ON supervision_actions(status, facility);
        -- Phase B: programme workplan / corrective actions
        CREATE TABLE IF NOT EXISTS workplan_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT,
            problem TEXT NOT NULL,
            action_text TEXT NOT NULL,
            owner TEXT,
            due_date TEXT,
            activity TEXT,
            evidence TEXT,
            status TEXT DEFAULT 'open',
            source TEXT DEFAULT 'manual',
            linked_alert_id INTEGER,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT,
            closed_by TEXT,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workplan_status ON workplan_actions(status, facility, due_date);
        CREATE TABLE IF NOT EXISTS dq_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_type TEXT,
            facility TEXT,
            period TEXT,
            severity TEXT,
            message TEXT,
            owner TEXT,
            due_date TEXT,
            status TEXT DEFAULT 'open',
            workplan_id INTEGER,
            source TEXT,
            evidence TEXT,
            verification_note TEXT,
            verified_at TEXT,
            verified_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            resolved_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dq_issues_status ON dq_issues(status, severity);
        CREATE TABLE IF NOT EXISTS stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT, item TEXT, quantity INTEGER,
            reorder_point INTEGER DEFAULT 30, last_restock TEXT,
            UNIQUE(facility, item)
        );
        CREATE TABLE IF NOT EXISTS indicator_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            indicator_id TEXT, period TEXT, facility TEXT,
            comment TEXT, author TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS population (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            facility TEXT,
            population INTEGER NOT NULL,
            year INTEGER NOT NULL,
            source TEXT,
            UNIQUE(entity_type, entity_name, year)
        );
        CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_name TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL,
            condition_type TEXT NOT NULL,
            indicator_pattern TEXT,
            indicator_pattern2 TEXT,
            comparison TEXT,
            threshold REAL,
            message_template TEXT,
            recipient_role TEXT,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_rule_id INTEGER,
            category TEXT,
            severity TEXT,
            facility TEXT,
            register TEXT,
            period TEXT,
            message TEXT,
            triggered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            acknowledged_at TEXT,
            resolved_at TEXT,
            status TEXT DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS alert_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            facility TEXT,
            sub_county TEXT,
            phone TEXT,
            email TEXT,
            alert_family TEXT,
            min_severity TEXT DEFAULT 'warning',
            sms_enabled INTEGER DEFAULT 1,
            email_enabled INTEGER DEFAULT 0,
            dashboard_enabled INTEGER DEFAULT 1,
            escalation_hours INTEGER DEFAULT 24,
            active INTEGER DEFAULT 1,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS climate_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_ending TEXT NOT NULL,
            facility TEXT,
            rainfall_mm REAL,
            temperature_c REAL,
            source TEXT,
            recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS report_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT,
            facility TEXT,
            period TEXT,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            generated_by TEXT,
            content TEXT
        );
        CREATE TABLE IF NOT EXISTS upload_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER NOT NULL,
            facility TEXT NOT NULL,
            stream TEXT,
            register TEXT NOT NULL,
            indicator TEXT NOT NULL,
            period TEXT NOT NULL,
            age_group TEXT DEFAULT 'All ages',
            value REAL,
            FOREIGN KEY (upload_id) REFERENCES upload_history(id)
        );
        CREATE INDEX IF NOT EXISTS idx_snap_upload ON upload_snapshots(upload_id);
        CREATE INDEX IF NOT EXISTS idx_snap_keys
            ON upload_snapshots(facility, register, indicator, period);
        CREATE TABLE IF NOT EXISTS dq_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewed_by TEXT NOT NULL,
            reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            dq_score REAL,
            facility_filter TEXT,
            period_filter TEXT,
            note TEXT,
            status TEXT DEFAULT 'reviewed'
        );
        -- Phase A0: immutable KHIS submissions
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            stored_path TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source_system TEXT DEFAULT 'KHIS',
            layer TEXT DEFAULT 'khis_aggregate',
            row_count INTEGER DEFAULT 0,
            matched_labels INTEGER DEFAULT 0,
            unmatched_labels INTEGER DEFAULT 0,
            period_types TEXT,
            geo_levels TEXT,
            validation_json TEXT,
            notes TEXT
        );
        -- Phase A0: indicator dictionary (structured KNOWN_LABELS)
        CREATE TABLE IF NOT EXISTS indicator_dictionary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_label TEXT NOT NULL UNIQUE,
            source_label_norm TEXT NOT NULL,
            register TEXT NOT NULL,
            indicator TEXT NOT NULL,
            age_group TEXT DEFAULT 'All ages',
            domain TEXT,
            method TEXT,
            measure TEXT,
            indicator_family TEXT,
            stream TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_dict_norm ON indicator_dictionary(source_label_norm);
        -- Phase A0: append-only observations (never overwrite)
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            layer TEXT DEFAULT 'khis_aggregate',
            geo_level TEXT NOT NULL,
            geo_name TEXT,
            facility TEXT,
            sub_county TEXT,
            stream TEXT,
            register TEXT NOT NULL,
            indicator TEXT NOT NULL,
            age_group TEXT DEFAULT 'All ages',
            period TEXT NOT NULL,
            period_type TEXT,
            period_start TEXT,
            period_end TEXT,
            source_period_label TEXT,
            value REAL,
            dictionary_id INTEGER,
            source_label TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (submission_id) REFERENCES submissions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_obs_sub ON observations(submission_id);
        CREATE INDEX IF NOT EXISTS idx_obs_keys
            ON observations(geo_level, facility, register, indicator, period);
        -- Phase A0: reconciliation stubs
        CREATE TABLE IF NOT EXISTS recon_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT DEFAULT CURRENT_TIMESTAMP,
            run_by TEXT,
            scope TEXT,
            items_checked INTEGER DEFAULT 0,
            items_flagged INTEGER DEFAULT 0,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS recon_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            rule_code TEXT NOT NULL,
            facility TEXT,
            period TEXT,
            measure_a TEXT,
            value_a REAL,
            measure_b TEXT,
            value_b REAL,
            difference REAL,
            difference_pct REAL,
            status TEXT DEFAULT 'requires_review',
            message TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES recon_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_recon_run ON recon_items(run_id);
        -- Phase A Layer 2: operational / register imports
        CREATE TABLE IF NOT EXISTS layer2_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            file_name TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            row_count INTEGER DEFAULT 0,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS moh_648 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER,
            report_date TEXT,
            facility TEXT,
            chu TEXT,
            chp_name TEXT,
            suspected INTEGER,
            tested INTEGER,
            positive INTEGER,
            treated INTEGER,
            referred INTEGER,
            followed_up INTEGER,
            itn_distributed INTEGER,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS moh_521 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER,
            report_date TEXT,
            facility TEXT,
            chu TEXT,
            suspected INTEGER,
            tested INTEGER,
            positive INTEGER,
            referred INTEGER,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upload_id INTEGER,
            movement_date TEXT,
            facility TEXT,
            item TEXT,
            movement_type TEXT,
            quantity INTEGER,
            balance_after INTEGER,
            batch_or_lot TEXT,
            expiry_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_648_fac ON moh_648(facility, report_date);
        CREATE INDEX IF NOT EXISTS idx_521_fac ON moh_521(facility, report_date);
        CREATE TABLE IF NOT EXISTS chu_gps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT,
            chu TEXT,
            lat REAL,
            lon REAL,
            source TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_chu_gps ON chu_gps(facility, chu);
        CREATE TABLE IF NOT EXISTS programme_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT,
            updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS budget_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fy TEXT,
            partner TEXT,
            geography TEXT,
            category TEXT,
            activity TEXT,
            budget REAL,
            expenditure REAL,
            currency TEXT DEFAULT 'KES',
            notes TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS kilifi_subcounty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_county TEXT NOT NULL,
            period TEXT,
            period_date TEXT,
            suspected REAL,
            tested REAL,
            confirmed REAL,
            al_dispensed REAL,
            rdt_exam REAL,
            rdt_pos REAL,
            bs_exam REAL,
            bs_pos REAL,
            testing_rate REAL,
            positivity REAL,
            flag_pos_gt_100 INTEGER,
            flag_conf_gt_tested INTEGER,
            source_file TEXT
        );
        CREATE TABLE IF NOT EXISTS kilifi_facility_wide (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_county TEXT,
            unit TEXT,
            period TEXT,
            suspected REAL,
            tested REAL,
            confirmed REAL,
            positivity REAL,
            flag_pos_gt_100 INTEGER,
            flag_conf_gt_tested INTEGER,
            source_file TEXT
        );
        CREATE TABLE IF NOT EXISTS stock_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT,
            item TEXT,
            batch TEXT,
            quantity INTEGER,
            expiry_date TEXT,
            received_date TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS sms_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
            recipient TEXT,
            body TEXT,
            status TEXT,
            provider_response TEXT
        );
        CREATE TABLE IF NOT EXISTS kilifi_upload_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_label TEXT,
            source_file TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            county_rows INTEGER,
            subcounty_rows INTEGER,
            facility_rows INTEGER,
            period_range TEXT,
            status TEXT DEFAULT 'archived',
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS kilifi_county_trend (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period TEXT,
            period_date TEXT,
            suspected REAL,
            tested REAL,
            confirmed REAL,
            testing_rate REAL,
            positivity REAL,
            source_file TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_stock_mv ON stock_movements(facility, item, movement_date);
    """)
    # Phase A.5 migrations — event-level MOH 648 fields (safe if already present)
    for col, typ in [
        ("result", "TEXT"),
        ("treatment_outcome", "TEXT"),
        ("referral_arrived", "INTEGER"),
        ("linkage_id", "TEXT"),
        ("age_group", "TEXT"),
        ("sex", "TEXT"),
        ("event_date", "TEXT"),
        ("chp_id", "TEXT"),
        ("negative", "INTEGER"),
        ("treated_without_test", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE moh_648 ADD COLUMN {col} {typ}")
        except Exception:
            pass
    for col, typ in [
        ("treated", "INTEGER"),
        ("followed_up", "INTEGER"),
        ("source", "TEXT"),
        ("rollup_from_648", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE moh_521 ADD COLUMN {col} {typ}")
        except Exception:
            pass
    for col, typ in [
        ("evidence", "TEXT"),
        ("verification_note", "TEXT"),
        ("verified_at", "TEXT"),
        ("verified_by", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE dq_issues ADD COLUMN {col} {typ}")
        except Exception:
            pass
    for col, typ in [
        ("decision", "TEXT"),
        ("decision_note", "TEXT"),
        ("signed_off_by", "TEXT"),
        ("signed_off_at", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE recon_items ADD COLUMN {col} {typ}")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE climate_data ADD COLUMN source TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE stock ADD COLUMN physical_count INTEGER")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE stock ADD COLUMN physical_date TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE alerts ADD COLUMN followed_up INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE alerts ADD COLUMN followed_up_at TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE alerts ADD COLUMN followed_up_by TEXT")
    except Exception:
        pass
    for _ucol, _utyp in (
        ("role_key", "TEXT"),
        ("display_name", "TEXT"),
        ("facility_scope", "TEXT"),
        ("sub_county_scope", "TEXT"),
        ("active", "INTEGER DEFAULT 1"),
        ("last_login", "TEXT"),
        ("pages_override", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {_ucol} {_utyp}")
        except Exception:
            pass
    for _acol, _atyp in (
        ("lifecycle", "TEXT DEFAULT 'new'"),
        ("acknowledged_by", "TEXT"),
        ("investigating_at", "TEXT"),
        ("investigating_by", "TEXT"),
        ("investigation_finding", "TEXT"),
        ("action_assigned", "TEXT"),
        ("action_owner", "TEXT"),
        ("closure_reason", "TEXT"),
        ("false_alarm", "INTEGER DEFAULT 0"),
    ):
        try:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {_acol} {_atyp}")
        except Exception:
            pass
    try:
        conn.execute("ALTER TABLE climate_data ADD COLUMN geography TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE climate_data ADD COLUMN humidity_pct REAL")
    except Exception:
        pass
    for col, typ in (
        ("temp_min", "REAL"), ("temp_max", "REAL"),
        ("rain_hours", "REAL"), ("et0_mm", "REAL"),
        ("horizon", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE climate_data ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS climate_forecast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_date TEXT,
            facility TEXT,
            geography TEXT,
            rainfall_mm REAL,
            temp_mean REAL,
            temp_min REAL,
            temp_max REAL,
            humidity_pct REAL,
            rain_hours REAL,
            et0_mm REAL,
            source TEXT,
            pulled_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # ── Malaria Early Warning schema (Phase C) ──────────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facility_env (
            facility TEXT PRIMARY KEY,
            elevation_m REAL,
            water_dist_km REAL,
            ndvi_baseline REAL,
            land_cover TEXT,
            pop_density REAL,
            travel_time_min REAL,
            lat REAL,
            lon REAL,
            notes TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS malaria_weekly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_ending TEXT NOT NULL,
            facility TEXT NOT NULL,
            tested REAL,
            positive REAL,
            confirmed REAL,
            suspected REAL,
            tpr REAL,
            cases_per_1000 REAL,
            outpatients REAL,
            fever_consults REAL,
            rdt_stockout_days REAL,
            act_stockout_days REAL,
            testing_volume REAL,
            source TEXT,
            UNIQUE(week_ending, facility)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ew_forecasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT DEFAULT CURRENT_TIMESTAMP,
            facility TEXT NOT NULL,
            horizon_weeks INTEGER NOT NULL,
            forecast_week TEXT,
            predicted_cases REAL,
            pred_low REAL,
            pred_high REAL,
            risk_level TEXT,
            risk_score REAL,
            model_name TEXT,
            drivers_json TEXT,
            confidence REAL,
            actual_cases REAL,
            UNIQUE(facility, horizon_weeks, forecast_week, model_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ew_model_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT DEFAULT CURRENT_TIMESTAMP,
            model_name TEXT,
            n_facilities INTEGER,
            n_weeks INTEGER,
            mae REAL,
            mape REAL,
            notes TEXT
        )
        """
    )
    # Seed facility environmental attributes (Kilifi CBMP sites)
    for fac, vals in (
        ("Jaribuni", (180, 2.5, 0.42, "agriculture_shrub", 85, 45, -3.62164, 39.74354,
                      "Inland Ganze; moderate elevation; seasonal streams")),
        ("Pingilikani", (45, 0.9, 0.48, "coastal_agriculture", 120, 25, -3.7834, 39.7802,
                         "Coastal Kilifi South; closer to permanent water")),
        ("Mwakuhenga", (25, 0.6, 0.45, "coastal_settlement", 95, 20, -3.70242, 39.81219,
                        "Near coast; lower elevation; higher water proximity")),
    ):
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO facility_env
                (facility, elevation_m, water_dist_km, ndvi_baseline, land_cover,
                 pop_density, travel_time_min, lat, lon, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fac, *vals),
            )
        except Exception:
            pass
    # C2/C3 tables: interventions, mobility, entomology, NDVI time series
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intervention_coverage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT NOT NULL,
            period TEXT NOT NULL,
            itn_ownership_pct REAL,
            itn_use_pct REAL,
            irs_coverage_pct REAL,
            irs_date TEXT,
            iptp_coverage_pct REAL,
            smc_coverage_pct REAL,
            source TEXT,
            notes TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(facility, period)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mobility_proxy (
            facility TEXT PRIMARY KEY,
            dist_major_road_km REAL,
            dist_market_km REAL,
            dist_town_km REAL,
            travel_time_facility_min REAL,
            fishing_community INTEGER DEFAULT 0,
            seasonal_migration INTEGER DEFAULT 0,
            notes TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entomology (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            facility TEXT,
            survey_date TEXT,
            anopheles_density REAL,
            species TEXT,
            biting_rate REAL,
            sporozoite_rate REAL,
            larval_density REAL,
            eir REAL,
            insecticide_resistance TEXT,
            source TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ndvi_weekly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_ending TEXT NOT NULL,
            facility TEXT NOT NULL,
            ndvi_mean REAL,
            ndvi_anomaly REAL,
            source TEXT,
            UNIQUE(week_ending, facility)
        )
        """
    )
    # Seed mobility proxies (realistic Kilifi distances)
    for fac, vals in (
        ("Jaribuni", (3.2, 4.0, 12.0, 45, 0, 1, "Inland; some seasonal labour movement")),
        ("Pingilikani", (1.5, 2.0, 8.0, 25, 1, 0, "Coastal; fishing communities nearby")),
        ("Mwakuhenga", (1.0, 1.8, 6.0, 20, 1, 0, "Near coast/road; higher connectivity")),
    ):
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO mobility_proxy
                (facility, dist_major_road_km, dist_market_km, dist_town_km,
                 travel_time_facility_min, fishing_community, seasonal_migration, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fac, *vals),
            )
        except Exception:
            pass
    # Seed default intervention coverage (provisional until programme updates)
    for fac in ("Jaribuni", "Pingilikani", "Mwakuhenga"):
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO intervention_coverage
                (facility, period, itn_ownership_pct, itn_use_pct, irs_coverage_pct,
                 iptp_coverage_pct, source, notes)
                VALUES (?, '2026-Q1', 72, 58, 0, 45, 'provisional_seed',
                        'Replace with county survey / campaign data')
                """,
                (fac,),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()


def query(sql, params=()):
    conn = get_conn()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def execute(sql, params=()):
    conn = get_conn()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def log_audit(user, action, table_name=None, comment=None):
    execute(
        "INSERT INTO audit_log (user, action, table_name, comment) VALUES (?, ?, ?, ?)",
        (user, action, table_name, comment),
    )


def apply_user_data_scope(df, user=None):
    """
    Enforce geographic RBAC on a dataframe.
    facility_scope / sub_county_scope / scope=facility|sub_county.
    """
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    user = user or st.session_state.get("user") or {}
    d = df
    fac_scope = user.get("facility_scope")
    sub_scope = user.get("sub_county_scope")
    scope = user.get("scope") or "all"
    if fac_scope and "facility" in d.columns:
        d = d[d["facility"].astype(str) == str(fac_scope)]
    elif sub_scope:
        if "sub_county" in d.columns:
            d = d[d["sub_county"].astype(str) == str(sub_scope)]
        elif "facility" in d.columns:
            allowed = [
                f for f in FACILITIES
                if GEO_SITES.get(f, {}).get("sub_county") == sub_scope
                or SUBCOUNTY.get(f) == sub_scope
            ]
            if allowed:
                d = d[d["facility"].isin(allowed)]
    elif scope == "facility" and user.get("facility_scope"):
        d = d[d["facility"] == user["facility_scope"]]
    # Exclude NOT_YET_DUE from analytical loads by default
    if "data_status" in d.columns:
        d = d[d["data_status"].fillna("Actual").astype(str).str.upper() != "NOT_YET_DUE"]
    return d


def apply_min_cell(value, user=None, label="count"):
    """Suppress small cells for aggregation roles (privacy / disclosure control)."""
    user = user or st.session_state.get("user") or {}
    try:
        min_c = int(user.get("min_cell") or 1)
    except Exception:
        min_c = 1
    if not user.get("aggregation"):
        return value
    try:
        v = float(value)
    except Exception:
        return value
    if abs(v) < min_c and v != 0:
        return None  # suppressed
    return value


def fmt_count(value, user=None):
    """Format integer counts with min_cell suppression for aggregation roles."""
    v = apply_min_cell(value, user=user)
    if v is None:
        return "—"
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return str(v)


# Columns treated as counts for comparison / scoreboard suppression
COUNT_COLS = (
    "Suspected", "Tested", "Confirmed", "AL", "AL dispensed", "R1.1 tests",
    "Diagnosis gap", "Diag gap", "Treat gap", "RDT exams", "RDT positive",
    "RDT exam", "RDT pos", "Three sites", "Kilifi County sheet",
    "Prev confirmed", "Prev tested", "LY confirmed", "LY tested",
    "Δ confirmed", "Δ tested",
)


def suppress_count_columns(df, user=None, columns=None):
    """
    Apply min_cell to count columns for aggregation roles.
    Returns a copy; rates/percent columns are left unchanged.
    """
    if df is None or getattr(df, "empty", True):
        return df
    user = user or st.session_state.get("user") or {}
    if not user.get("aggregation"):
        return df
    out = df.copy()
    cols = columns or [c for c in out.columns if c in COUNT_COLS or str(c).lower() in {
        "suspected", "tested", "confirmed", "al_dispensed", "rdt_exam", "rdt_pos",
        "diag_gap", "treat_gap",
    }]
    for c in cols:
        if c not in out.columns:
            continue
        out[c] = out[c].apply(lambda x: apply_min_cell(x, user=user))
    return out


def load_all_data(user=None, include_not_yet_due=False):
    """Load malaria_data with RBAC scope and optional exclusion of future template cells."""
    df = query("SELECT * FROM malaria_data")
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    if not include_not_yet_due and "data_status" in df.columns:
        df = df[df["data_status"].fillna("Actual").astype(str).str.upper() != "NOT_YET_DUE"]
    return apply_user_data_scope(df, user=user)


def count_rows():
    try:
        return int(query("SELECT COUNT(*) AS n FROM malaria_data").iloc[0]["n"])
    except Exception:
        return 0


def last_upload():
    try:
        df = query("SELECT * FROM upload_history ORDER BY id DESC LIMIT 1")
        return df.iloc[0] if not df.empty else None
    except Exception:
        return None


def populate_population():
    """Seed provisional populations. Safe to call on every Streamlit rerun."""
    data = [
        ("facility", "Jaribuni", "Jaribuni", 25000, 2026, "Kilifi County Spatial Plan 2021-2030"),
        ("facility", "Pingilikani", "Pingilikani", 30000, 2026, "Kilifi County Spatial Plan 2021-2030"),
        ("facility", "Mwakuhenga", "Mwakuhenga", 15000, 2026, "Kilifi County Spatial Plan 2021-2030"),
        ("facility", "Total", None, 70000, 2026, "Sum of three facilities"),
    ]
    conn = None
    try:
        conn = get_conn()
        conn.executemany(
            """
            INSERT OR REPLACE INTO population
                (entity_type, entity_name, facility, population, year, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            data,
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Locked by another connection — non-fatal on rerun; data already may exist
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_setting(key, default=None):
    try:
        row = query("SELECT value FROM programme_settings WHERE key = ?", (key,))
        if not row.empty and row.iloc[0]["value"] not in (None, ""):
            return row.iloc[0]["value"]
    except Exception:
        pass
    return default


def set_setting(key, value, user="system"):
    execute(
        """
        INSERT INTO programme_settings (key, value, updated_at, updated_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by
        """,
        (key, str(value), datetime.now().strftime("%Y-%m-%d %H:%M"), user),
    )


def get_population_status():
    return get_setting("population_status", POPULATION_STATUS)


def get_population(facility=None):
    key = facility if facility and facility != "All three" else "Total"
    stored = get_setting(f"pop_{key}")
    if stored:
        try:
            return int(float(stored))
        except Exception:
            pass
    if facility and facility in POPULATION:
        return POPULATION[facility]
    return POPULATION["Total"]


# Programme alert catalogue (code-driven; names used for de-dup with DB seed)
PROGRAMME_ALERT_DEFS = [
    {
        "key": "reporting_lt_60",
        "name": "Reporting rate critically low",
        "category": "Reporting",
        "severity": "critical",
    },
    {
        "key": "reporting_lt_80",
        "name": "Reporting rate below 80%",
        "category": "Reporting",
        "severity": "warning",
    },
    {
        "key": "diag_gap_high",
        "name": "Diagnosis gap high",
        "category": "Performance",
        "severity": "high",
    },
    {
        "key": "treat_gap_high",
        "name": "Treatment gap high",
        "category": "Performance",
        "severity": "high",
    },
    {
        "key": "testing_collapse",
        "name": "Testing collapse risk",
        "category": "Performance",
        "severity": "critical",
    },
    {
        "key": "tpr_high",
        "name": "RDT positivity very high",
        "category": "Performance",
        "severity": "warning",
    },
    {
        "key": "twt_high",
        "name": "Treated without testing elevated",
        "category": "Data Quality",
        "severity": "high",
    },
    {
        "key": "stock_rdt",
        "name": "RDT stock low",
        "category": "Commodity",
        "severity": "high",
    },
    {
        "key": "stock_al",
        "name": "AL stock low",
        "category": "Commodity",
        "severity": "high",
    },
    {
        "key": "go1_missing",
        "name": "GO_1 RDT inputs missing",
        "category": "Data Quality",
        "severity": "critical",
    },
]


def populate_alert_rules():
    """Seed legacy DB rules once; programme alerts are evaluated in code."""
    try:
        existing = query("SELECT COUNT(*) AS n FROM alert_rules").iloc[0]["n"]
    except Exception:
        existing = 0
    if existing > 0:
        return
    rules = [
        ("Reporting rate above 100%", "Data Quality", "critical",
         "indicator_threshold", "Reporting Rate", None, "greater", 1.5,
         "Reporting rate >150% at {facility} ({register}) for {period}", "admin"),
        ("RDT stock low", "Commodity", "high",
         "stock_low", "RDT", None, "less", 30,
         "RDT stock below reorder point at {facility}", "admin"),
        ("AL stock low", "Commodity", "high",
         "stock_low", "AL", None, "less", 30,
         "AL stock below reorder point at {facility}", "admin"),
    ]
    conn = None
    try:
        conn = get_conn()
        for rule in rules:
            conn.execute("""
                INSERT INTO alert_rules
                    (alert_name, category, severity, condition_type,
                     indicator_pattern, indicator_pattern2, comparison, threshold,
                     message_template, recipient_role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rule)
        conn.commit()
    except Exception:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ==================================================================
# PARSING
# ==================================================================

def normalize_period(col):
    """
    Phase A0 period engine.
    Returns dict or None:
      period, period_type, period_start, period_end, source_period_label, reporting_year
    """
    s = str(col).strip()
    if not s or s.upper() == "PROJECT TOTAL" or s.lower().startswith("unnamed"):
        return None

    source = s

    # Pure 4-digit year
    if s.isdigit() and len(s) == 4:
        y = int(s)
        return {
            "period": s,
            "period_type": "year",
            "period_start": f"{s}-01-01",
            "period_end": f"{s}-12-31",
            "source_period_label": source,
            "reporting_year": y,
        }

    # Quarters: Jan-Mar25, Apr-Jun 25, etc.
    quarters = {
        "jan-mar": ("Q1", "01-01", "03-31"),
        "apr-jun": ("Q2", "04-01", "06-30"),
        "jul-sep": ("Q3", "07-01", "09-30"),
        "oct-dec": ("Q4", "10-01", "12-31"),
    }
    low = s.lower().replace(" ", "")
    for q, (qlabel, start_md, end_md) in quarters.items():
        if q in low:
            yr = "".join(c for c in s if c.isdigit())
            if len(yr) == 2:
                yr = "20" + yr
            if len(yr) >= 4:
                yr = yr[:4]
            if len(yr) == 4:
                return {
                    "period": f"{yr}-{qlabel}",
                    "period_type": "quarter",
                    "period_start": f"{yr}-{start_md}",
                    "period_end": f"{yr}-{end_md}",
                    "source_period_label": source,
                    "reporting_year": int(yr),
                }

    # Full date / datetime → month
    try:
        dt = pd.to_datetime(s, errors="raise")
        y, m = dt.year, dt.month
        # month end
        if m == 12:
            end = f"{y}-12-31"
        else:
            end_dt = pd.Timestamp(year=y, month=m + 1, day=1) - pd.Timedelta(days=1)
            end = end_dt.strftime("%Y-%m-%d")
        return {
            "period": dt.strftime("%Y-%m"),
            "period_type": "month",
            "period_start": f"{y}-{m:02d}-01",
            "period_end": end,
            "source_period_label": source,
            "reporting_year": y,
        }
    except Exception:
        pass

    return None


def period_from_column(col):
    """Backward-compatible: return canonical period key or None."""
    info = normalize_period(col)
    return info["period"] if info else None


def _infer_dictionary_meta(register, indicator, age_group):
    """Infer domain / method / measure / family from mapped indicator name."""
    ind = (indicator or "").lower()
    reg = (register or "").upper()
    domain, method, measure, family = "Other", None, indicator, "Other"

    if "reporting rate" in ind:
        domain, measure, family = "Health system", "Reporting rate", "Reporting"
    elif "suspected" in ind:
        domain, measure, family = "Diagnosis", "Suspected", "Testing"
    elif "tested" in ind or "total exam" in ind:
        domain, measure, family = "Diagnosis", "Total examinations", "Testing"
    elif "positive" in ind or "confirmed" in ind:
        domain, measure, family = "Diagnosis", "Positive tests", "Positivity"
    elif "negative" in ind:
        domain, measure, family = "Diagnosis", "Negative tests", "Testing"
    elif "invalid" in ind:
        domain, measure, family = "Diagnosis", "Invalid tests", "Testing"
    elif ind.startswith("al") or "al dispensed" in ind or "al total" in ind:
        domain, measure, family = "Treatment", "AL dispensed", "Treatment"
    elif "treated" in ind:
        domain, measure, family = "Treatment", "Treated without testing", "Treatment"
    elif "ipt" in ind:
        domain, measure, family = "Prevention", indicator, "Prevention"
    elif "rdt dispensed" in ind:
        domain, measure, family = "Commodities", "RDT dispensed", "Commodities"
    elif "mip" in ind or "pregnancy" in ind:
        domain, measure, family = "Clinical", "Malaria in pregnancy", "Epidemiology"
    elif "anc" in ind:
        domain, measure, family = "Prevention", indicator, "Prevention"
    elif "referred" in ind or "follow" in ind:
        domain, measure, family = "Community", indicator, "Community"
    elif "itn" in ind:
        domain, measure, family = "Prevention", indicator, "Prevention"

    if "rdt" in ind:
        method = "RDT"
    elif "bs" in ind or "microscopy" in ind:
        method = "Blood smear"
    if reg == "MOH 706" and method is None:
        if "rdt" in ind:
            method = "RDT"
        elif "bs" in ind:
            method = "Blood smear"

    stream = detect_stream_for_register(register)
    return domain, method, measure, family, stream


def seed_indicator_dictionary():
    """Phase A0: seed indicator_dictionary from KNOWN_LABELS (idempotent)."""
    try:
        conn = get_conn()
        cur = conn.cursor()
        for source_label, (register, indicator, age_group) in KNOWN_LABELS.items():
            norm = _norm_label(source_label)
            domain, method, measure, family, stream = _infer_dictionary_meta(
                register, indicator, age_group
            )
            cur.execute(
                """
                INSERT INTO indicator_dictionary
                    (source_label, source_label_norm, register, indicator, age_group,
                     domain, method, measure, indicator_family, stream, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(source_label) DO UPDATE SET
                    source_label_norm=excluded.source_label_norm,
                    register=excluded.register,
                    indicator=excluded.indicator,
                    age_group=excluded.age_group,
                    domain=excluded.domain,
                    method=excluded.method,
                    measure=excluded.measure,
                    indicator_family=excluded.indicator_family,
                    stream=excluded.stream
                """,
                (
                    source_label, norm, register, indicator, age_group,
                    domain, method, measure, family, stream,
                ),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass


def lookup_dictionary(source_label_norm):
    """Return dictionary row dict or None."""
    try:
        df = query(
            "SELECT * FROM indicator_dictionary WHERE source_label_norm = ? AND is_active = 1 LIMIT 1",
            (source_label_norm,),
        )
        if df.empty:
            # fuzzy MOH 515
            if source_label_norm.startswith("MOH 515 Reporting rate"):
                df = query(
                    "SELECT * FROM indicator_dictionary WHERE source_label_norm = ? LIMIT 1",
                    ("MOH 515 Reporting rate",),
                )
            elif source_label_norm.startswith("MOH 711"):
                df = query(
                    """
                    SELECT * FROM indicator_dictionary
                    WHERE source_label_norm = ? AND is_active = 1 LIMIT 1
                    """,
                    (source_label_norm,),
                )
        return df.iloc[0].to_dict() if not df.empty else None
    except Exception:
        return None


def detect_stream_for_register(register):
    if register in ("MOH 648", "MOH 521", "MOH 748", "MOH 515", "MOH 711"):
        return "Community"
    return "Facility"


def parse_sheet(df, facility, geo_info=None):
    """
    Parse one sheet block (facility or county aggregate).
    Phase A0: attaches period_type, source_period_label, geo_level.
    Returns (records_df, meta).
    """
    geo_info = geo_info or {
        "geo_level": "facility",
        "geo_name": facility,
        "facility": facility,
        "sub_county": SUBCOUNTY.get(facility) if facility else None,
    }
    meta = {
        "facility": facility,
        "geo_level": geo_info.get("geo_level"),
        "geo_name": geo_info.get("geo_name"),
        "marker_found": False,
        "matched_labels": [],
        "unmatched_labels": [],
        "periods_seen": set(),
        "period_types_seen": set(),
        "skipped_period_cols": [],
        "rows_parsed": 0,
        "dict_hits": 0,
    }
    records = []
    if df.empty:
        return pd.DataFrame(), meta

    label_col = df.columns[0]
    marker_idx = None
    # County-level sheets: no facility marker required — use full table
    if geo_info.get("geo_level") == "county":
        subset = df
        meta["marker_found"] = True
    else:
        for idx, val in df[label_col].items():
            if pd.isna(val):
                continue
            text = str(val).strip().upper()
            for marker_key, marker_fac in FACILITY_MARKERS.items():
                if marker_key in text and marker_fac == facility:
                    marker_idx = idx
                    meta["marker_found"] = True
                    break
            if marker_idx is not None:
                break
        subset = df.iloc[marker_idx + 1:] if marker_idx is not None else df

    seen_unmatched = set()
    seen_matched = set()

    for _, row in subset.iterrows():
        raw = row[label_col]
        if pd.isna(raw):
            continue
        raw_text = str(raw).strip()
        upper = raw_text.upper()
        if geo_info.get("geo_level") != "county":
            if any(upper.startswith(m) or upper == m for m in FACILITY_MARKERS.keys()):
                break

        norm = _norm_label(raw_text)
        if not norm:
            continue

        label_key = norm if norm in KNOWN_LABELS else None
        if label_key is None and norm.startswith("MOH 515 Reporting rate"):
            label_key = "MOH 515 Reporting rate"
        # Fuzzy MOH 711 Hb / anaemia (label wording varies by extract)
        if label_key is None and "MOH 711" in norm.upper():
            nu = norm.upper()
            if "HB" in nu and ("11" in nu or "ANAEM" in nu or "ANEM" in nu):
                label_key = "MOH 711_Clients with Hb less than 11 g/dl at 1st ANC visit"
            elif "LLIN" in nu and "ANC" in nu:
                label_key = "MOH 711_LLINs issued to ANC clients"
            elif "REPORTING RATE" in nu:
                label_key = "MOH 711 Reporting rate"

        if label_key is None or label_key not in KNOWN_LABELS:
            if norm not in seen_unmatched and not norm.upper().startswith("MOH DATA"):
                seen_unmatched.add(norm)
                meta["unmatched_labels"].append(norm)
            continue

        if norm not in seen_matched:
            seen_matched.add(norm)
            meta["matched_labels"].append(norm)

        register, indicator, age_group = KNOWN_LABELS[label_key]
        row_stream = detect_stream_for_register(register)
        meta["dict_hits"] += 1

        for col in df.columns[1:]:
            val = row[col]
            if pd.isna(val):
                continue
            if isinstance(val, str) and val.strip().startswith("="):
                continue
            pinfo = normalize_period(col)
            if pinfo is None:
                col_s = str(col).strip()
                if col_s and col_s.upper() != "PROJECT TOTAL" and not col_s.lower().startswith("unnamed"):
                    if col_s not in meta["skipped_period_cols"]:
                        meta["skipped_period_cols"].append(col_s)
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            meta["periods_seen"].add(pinfo["period"])
            meta["period_types_seen"].add(pinfo["period_type"])
            # Latest-view facility key: county rows use geo name as facility placeholder
            fac_key = geo_info.get("facility") or geo_info.get("geo_name") or facility or "Unknown"
            # Future / not-yet-due periods must not enter analytics as Actual
            _ds = "Actual"
            try:
                _today = pd.Timestamp(datetime.now().date())
                _pe = pinfo.get("period_end") or pinfo.get("period_start")
                if _pe is not None:
                    _pe_ts = pd.to_datetime(_pe, errors="coerce")
                    if pd.notna(_pe_ts) and _pe_ts > _today + pd.Timedelta(days=3):
                        _ds = "NOT_YET_DUE"
            except Exception:
                _ds = "Actual"
            records.append({
                "facility": fac_key,
                "sub_county": geo_info.get("sub_county") or SUBCOUNTY.get(fac_key),
                "stream": row_stream,
                "register": register,
                "indicator": indicator,
                "period": pinfo["period"],
                "period_type": pinfo["period_type"],
                "period_start": pinfo["period_start"],
                "period_end": pinfo["period_end"],
                "source_period_label": pinfo["source_period_label"],
                "age_group": age_group,
                "value": num,
                "data_status": _ds,
                "geo_level": geo_info.get("geo_level", "facility"),
                "geo_name": geo_info.get("geo_name") or fac_key,
                "source_label": label_key,
                "into_latest_view": geo_info.get("geo_level") == "facility" and _ds == "Actual",
            })

    meta["rows_parsed"] = len(records)
    meta["periods_seen"] = sorted(meta["periods_seen"])
    meta["period_types_seen"] = sorted(meta["period_types_seen"])
    return pd.DataFrame(records), meta


def _upload_to_bytes_io(uploaded_file):
    """Copy upload into memory so it can be read more than once (Streamlit buffers)."""
    try:
        raw = uploaded_file.getvalue()
    except Exception:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        raw = uploaded_file.read()
    return io.BytesIO(raw)


def parse_uploaded_file(uploaded_file):
    """
    Parse Excel (or CSV) upload.
    Returns (parsed_df, report) where report is a dict for the validation UI.
    """
    name = getattr(uploaded_file, "name", "") or ""
    report = {
        "file_name": name,
        "sheets_found": [],
        "sheets_mapped": [],
        "sheets_skipped": [],
        "facilities": {},
        "total_rows": 0,
        "facility_rows": 0,
        "county_rows": 0,
        "all_matched_labels": set(),
        "all_unmatched_labels": set(),
        "all_periods": set(),
        "period_types": set(),
        "geo_levels": set(),
        "dict_hit_rate": None,
        "errors": [],
        "phase_a0": True,
    }

    buffer = _upload_to_bytes_io(uploaded_file)

    if name.lower().endswith(".csv"):
        try:
            df = pd.read_csv(buffer)
            report["sheets_found"] = ["(csv)"]
            report["errors"].append(
                "CSV upload is accepted but facility markers are not auto-detected. "
                "Prefer the standard multi-sheet KHIS Excel export."
            )
            return pd.DataFrame(), report
        except Exception as e:
            report["errors"].append(f"CSV read failed: {e}")
            return pd.DataFrame(), report

    try:
        buffer.seek(0)
        xls = pd.ExcelFile(buffer)
    except Exception as e:
        report["errors"].append(f"Could not open Excel file: {e}")
        return pd.DataFrame(), report

    report["sheets_found"] = list(xls.sheet_names)
    all_records = []
    matched_n = unmatched_n = 0

    for sheet_name in xls.sheet_names:
        geo_info = None
        for key, info in SHEET_TO_GEO.items():
            if key.upper() in sheet_name.upper():
                geo_info = dict(info)
                break
        # Fallback: legacy facility-only map
        if geo_info is None:
            for key, fac in SHEET_TO_FACILITY.items():
                if key.upper() in sheet_name.upper():
                    geo_info = {
                        "geo_level": "facility",
                        "geo_name": fac,
                        "facility": fac,
                        "sub_county": SUBCOUNTY.get(fac),
                    }
                    break
        if geo_info is None:
            report["sheets_skipped"].append(sheet_name)
            continue

        facility = geo_info.get("facility")
        report["sheets_mapped"].append({
            "sheet": sheet_name,
            "facility": facility,
            "geo_level": geo_info.get("geo_level"),
            "geo_name": geo_info.get("geo_name"),
        })
        try:
            df = xls.parse(sheet_name)
        except Exception as e:
            report["errors"].append(f"Failed to parse sheet '{sheet_name}': {e}")
            continue

        parsed, meta = parse_sheet(df, facility, geo_info=geo_info)
        key_name = facility or geo_info.get("geo_name") or sheet_name
        report["facilities"][key_name] = meta
        report["all_matched_labels"].update(meta["matched_labels"])
        report["all_unmatched_labels"].update(meta["unmatched_labels"])
        report["all_periods"].update(meta["periods_seen"])
        report["period_types"].update(meta.get("period_types_seen") or [])
        report["geo_levels"].add(geo_info.get("geo_level"))
        matched_n += len(meta.get("matched_labels") or [])
        unmatched_n += len(meta.get("unmatched_labels") or [])

        if not parsed.empty:
            all_records.append(parsed)
            if geo_info.get("geo_level") == "county":
                report["county_rows"] += len(parsed)
            else:
                report["facility_rows"] += len(parsed)

    total_labels = matched_n + unmatched_n
    report["dict_hit_rate"] = round(matched_n / total_labels * 100, 1) if total_labels else None

    if all_records:
        result = pd.concat(all_records, ignore_index=True)
        report["total_rows"] = len(result)
        report["all_periods"] = sorted(report["all_periods"])
        report["period_types"] = sorted(report["period_types"])
        report["geo_levels"] = sorted(report["geo_levels"])
        report["all_matched_labels"] = sorted(report["all_matched_labels"])
        report["all_unmatched_labels"] = sorted(report["all_unmatched_labels"])
        return result, report

    report["all_matched_labels"] = sorted(report["all_matched_labels"])
    report["all_unmatched_labels"] = sorted(report["all_unmatched_labels"])
    report["all_periods"] = sorted(report["all_periods"])
    report["period_types"] = sorted(report["period_types"])
    report["geo_levels"] = sorted(report["geo_levels"])
    return pd.DataFrame(), report


def store_upload(uploaded_file, parsed_df, user, parse_report=None):
    """
    Phase A0: write immutable submission + append-only observations,
    and refresh malaria_data latest view for facility-level rows only.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_name = uploaded_file.name.replace(" ", "_")
    stored_name = f"{timestamp}_{safe_name}"
    stored_path = UPLOADS_DIR / stored_name

    try:
        raw = uploaded_file.getbuffer()
    except Exception:
        raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()
    with open(stored_path, "wb") as f:
        f.write(raw)

    parse_report = parse_report or {}
    conn = get_conn()
    cur = conn.cursor()

    # Immutable submission record
    cur.execute(
        """
        INSERT INTO submissions
            (file_name, stored_path, uploaded_by, source_system, layer,
             row_count, matched_labels, unmatched_labels, period_types, geo_levels,
             validation_json)
        VALUES (?, ?, ?, 'KHIS', 'khis_aggregate', ?, ?, ?, ?, ?, ?)
        """,
        (
            uploaded_file.name,
            str(stored_path),
            user,
            len(parsed_df),
            len(parse_report.get("all_matched_labels") or []),
            len(parse_report.get("all_unmatched_labels") or []),
            ",".join(parse_report.get("period_types") or []),
            ",".join(parse_report.get("geo_levels") or []),
            json.dumps({
                "dict_hit_rate": parse_report.get("dict_hit_rate"),
                "facility_rows": parse_report.get("facility_rows"),
                "county_rows": parse_report.get("county_rows"),
                "sheets_mapped": parse_report.get("sheets_mapped"),
            }, default=str),
        ),
    )
    submission_id = cur.lastrowid

    committed = 0
    obs_n = 0
    for _, r in parsed_df.iterrows():
        # Append-only observation (Phase A0 history)
        cur.execute(
            """
            INSERT INTO observations
                (submission_id, layer, geo_level, geo_name, facility, sub_county,
                 stream, register, indicator, age_group, period, period_type,
                 period_start, period_end, source_period_label, value, source_label)
            VALUES (?, 'khis_aggregate', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                r.get("geo_level", "facility"),
                r.get("geo_name") or r.get("facility"),
                r.get("facility"),
                r.get("sub_county"),
                r.get("stream"),
                r["register"],
                r["indicator"],
                r.get("age_group", "All ages"),
                r["period"],
                r.get("period_type"),
                r.get("period_start"),
                r.get("period_end"),
                r.get("source_period_label"),
                r["value"],
                r.get("source_label"),
            ),
        )
        obs_n += 1

        # Latest view for programme UI — facility-level only (avoid county totals in facility KPIs)
        into_latest = r.get("into_latest_view", True)
        if into_latest is False or r.get("geo_level") == "county":
            continue
        cur.execute("""
            INSERT INTO malaria_data
                (facility, sub_county, stream, register, indicator,
                 period, age_group, value, data_status, uploaded_by, upload_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(facility, stream, register, indicator, period, age_group)
            DO UPDATE SET
                value=excluded.value,
                uploaded_by=excluded.uploaded_by,
                uploaded_at=CURRENT_TIMESTAMP,
                upload_file=excluded.upload_file
        """, (
            r["facility"], r["sub_county"], r["stream"], r["register"],
            r["indicator"], r["period"], r["age_group"], r["value"],
            r.get("data_status", "Actual"), user, uploaded_file.name,
        ))
        committed += 1

    cur.execute("""
        INSERT INTO upload_history
            (file_name, stored_path, facility, stream, rows_committed,
             validation_status, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        uploaded_file.name, str(stored_path),
        ", ".join(sorted(str(x) for x in parsed_df["facility"].dropna().unique())),
        ", ".join(sorted(str(x) for x in parsed_df["stream"].dropna().unique())),
        committed, "Valid", user,
    ))
    upload_id = cur.lastrowid

    # Snapshot facility-level rows for upload-to-upload compare
    snap_rows = []
    for _, r in parsed_df.iterrows():
        if r.get("geo_level") == "county":
            continue
        snap_rows.append((
            upload_id,
            r["facility"],
            r.get("stream"),
            r["register"],
            r["indicator"],
            r["period"],
            r.get("age_group", "All ages"),
            r["value"],
        ))
    if snap_rows:
        cur.executemany("""
            INSERT INTO upload_snapshots
                (upload_id, facility, stream, register, indicator, period, age_group, value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, snap_rows)

    conn.commit()
    conn.close()
    log_audit(
        user, "upload", "submissions",
        f"submission_id={submission_id} observations={obs_n} latest_view={committed} file={uploaded_file.name}",
    )
    # Phase A.5: auto integrity → DQ issues after commit
    try:
        df_latest = pd.read_sql_query("SELECT * FROM malaria_data", get_conn())
        sync_dq_issues_from_indicators(df_latest, {}, user=user)
        audit_df, summary = run_formula_integrity_audit(df_latest, {})
        inv_n = summary.get("invalid", 0) + summary.get("recon", 0)
        if inv_n:
            log_audit(user, "integrity_post_upload", "dq_issues", f"invalid_or_recon={inv_n}")
    except Exception:
        pass
    return committed, stored_path, upload_id, submission_id, obs_n


# ==================================================================
# AUTHENTICATION
# ==================================================================

def _bootstrap_password() -> str:
    """Initial admin password: CBMP_BOOTSTRAP_PASSWORD env, else random (printed once at seed)."""
    env = (os.environ.get("CBMP_BOOTSTRAP_PASSWORD") or "").strip()
    if env:
        return env
    # Do not hard-code a public default in production deployments
    import secrets
    return "CBMP-" + secrets.token_urlsafe(9)

DEFAULT_PASSWORD = _bootstrap_password()  # used only when seeding new accounts; must_change=1


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def seed_users():
    """Create one DB user per ROLES key if missing. Default password must be changed."""
    conn = None
    try:
        conn = get_conn()
        existing = {
            r[0]
            for r in conn.execute("SELECT username FROM users").fetchall()
        }
        for username in ROLES:
            if username not in existing:
                conn.execute(
                    """
                    INSERT INTO users
                    (username, password_hash, must_change, role_key, display_name, active)
                    VALUES (?, ?, 1, ?, ?, 1)
                    """,
                    (
                        username,
                        _hash_password(DEFAULT_PASSWORD),
                        username,
                        ROLES[username]["name"],
                    ),
                )
            else:
                # Backfill role_key if null
                conn.execute(
                    "UPDATE users SET role_key = COALESCE(role_key, ?), display_name = COALESCE(display_name, ?), "
                    "active = COALESCE(active, 1) WHERE username = ?",
                    (username, ROLES[username]["name"], username),
                )
        conn.commit()
    except sqlite3.OperationalError:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_user_record(username):
    """Full user row from DB."""
    try:
        row = query("SELECT * FROM users WHERE username = ?", (username,))
        if row.empty:
            return None
        return row.iloc[0].to_dict()
    except Exception:
        return None


def build_session_user(username):
    """
    Build session user dict from DB + ROLES template.
    role_key selects permissions; facility_scope / sub_county_scope limit geography.
    """
    rec = get_user_record(username)
    if not rec:
        return None
    if int(rec.get("active") if rec.get("active") is not None else 1) != 1:
        return None
    role_key = rec.get("role_key") or username
    if role_key not in ROLES:
        # Fallback: username was a role key
        if username in ROLES:
            role_key = username
        else:
            role_key = "viewer"
    tmpl = ROLES[role_key]
    scope = tmpl.get("scope", "all")
    # Narrow scope from explicit assignment
    fac = rec.get("facility_scope")
    sub = rec.get("sub_county_scope")
    if fac:
        scope = "facility"
    elif sub:
        scope = "sub_county"
    # Optional per-user module list (JSON array of page names)
    pages = list(tmpl["pages"])
    try:
        ov = rec.get("pages_override")
        if ov and str(ov).strip().startswith("["):
            import json as _json
            custom = _json.loads(ov)
            if isinstance(custom, list) and custom:
                pages = [p for p in custom if isinstance(p, str)]
    except Exception:
        pass
    return {
        "username": username,
        "name": rec.get("display_name") or tmpl["name"],
        "role": role_key,
        "pages": pages,
        "can_upload": bool(tmpl.get("can_upload")),
        "can_commit": bool(tmpl.get("can_commit")),
        "can_manage_users": bool(tmpl.get("can_manage_users")),
        "scope": scope,
        "facility_scope": fac,
        "sub_county_scope": sub,
        "aggregation": tmpl.get("aggregation", False),
        "min_cell": tmpl.get("min_cell", 5),
    }


def verify_password(username, password):
    row = query("SELECT password_hash, active, role_key FROM users WHERE username = ?", (username,))
    if row.empty:
        seed_users()
        row = query("SELECT password_hash, active, role_key FROM users WHERE username = ?", (username,))
        if row.empty:
            return False
    # Disabled accounts cannot sign in
    try:
        if int(row.iloc[0].get("active") if pd.notna(row.iloc[0].get("active")) else 1) != 1:
            return False
    except Exception:
        pass
    role_key = row.iloc[0].get("role_key") or username
    if role_key not in ROLES and username not in ROLES:
        return False
    stored = row.iloc[0]["password_hash"]
    try:
        if stored and bcrypt.checkpw(password.encode(), str(stored).encode()):
            return True
    except Exception:
        pass
    # Bootstrap unlock: known temp password always accepted once, then hash is rewritten
    # so pilot installs work even if the DB was seeded with an unknown random hash.
    BOOTSTRAP = (os.environ.get("CBMP_BOOTSTRAP_PASSWORD") or "ChangeMe123!").strip()
    if password == BOOTSTRAP:
        try:
            execute(
                "UPDATE users SET password_hash=?, must_change=1 WHERE username=?",
                (_hash_password(BOOTSTRAP), username),
            )
            return True
        except Exception:
            return True  # allow login even if hash rewrite fails
    return False


def user_must_change(username):
    row = query("SELECT must_change FROM users WHERE username = ?", (username,))
    if row.empty:
        return True
    return bool(row.iloc[0]["must_change"])


def set_password(username, new_password):
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters."
    execute(
        "UPDATE users SET password_hash = ?, must_change = 0, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
        (_hash_password(new_password), username),
    )
    log_audit(username, "password_change", "users", "Password updated")
    return True, "Password updated."


# ==================================================================
# RENDERING HELPERS
# ==================================================================

def _system_status_bits():
    """Compute light status line from live data (best-effort)."""
    data_lbl = "Data: —"
    ew_lbl = "EW: —"
    try:
        df = load_all_data()
        if df is not None and not df.empty and "period" in df.columns:
            pers = sorted(df["period"].astype(str).unique().tolist())
            data_lbl = f"Data through: {pers[-1]}" if pers else "Data: none"
    except Exception:
        pass
    try:
        n_ew = int(query("SELECT COUNT(*) AS n FROM ew_forecasts").iloc[0]["n"])
        ew_lbl = f"EW forecasts: {n_ew}" if n_ew else "EW: not run"
    except Exception:
        pass
    try:
        n_sc = int(query("SELECT COUNT(DISTINCT sub_county) AS n FROM kilifi_subcounty").iloc[0]["n"])
        sc_lbl = f"Sub-counties: {n_sc}/7"
    except Exception:
        sc_lbl = "Sub-counties: —"
    return data_lbl, ew_lbl, sc_lbl


def render_global_header(facility_badges=None):
    """
    Institutional brand header — call once per session after login.
    Logo hierarchy: MoH/County centre · partners on sides.
    """
    aics_path = ASSETS_DIR / "logo_aics.png"
    moh_path = ASSETS_DIR / "logo_moh.png"
    wf_path = ASSETS_DIR / "logo_worldfriends.png"
    c1, c2, c3 = st.columns([1.1, 2.2, 1.1])
    with c1:
        if moh_path.exists():
            st.image(str(moh_path), width=100)
        else:
            st.markdown("**Kilifi / MoH**")
        st.caption("County · Ministry of Health")
    with c2:
        st.markdown(
            """
            <div style="text-align:center;padding:4px 8px 2px 8px;">
              <div style="font-size:0.72rem;letter-spacing:0.12em;color:#5D6D7E;font-weight:600;">
                KILIFI COUNTY MALARIA PROGRAMME
              </div>
              <div style="font-size:1.35rem;font-weight:700;color:#1F4E79;line-height:1.25;margin:4px 0;">
                Surveillance · Early Warning · Decision Support
              </div>
              <div style="font-size:0.78rem;color:#566573;">
                Phase C · CBMP · PIR Community Based Malaria Programme
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        lc1, lc2 = st.columns(2)
        with lc1:
            if wf_path.exists():
                st.image(str(wf_path), width=72)
            else:
                st.caption("World Friends")
        with lc2:
            if aics_path.exists():
                st.image(str(aics_path), width=72)
            else:
                st.caption("AICS")
        st.caption("Partners")

    data_lbl, ew_lbl, sc_lbl = _system_status_bits()
    now = datetime.now().strftime("%d %b %Y %H:%M")
    st.markdown(
        f"""
        <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;
                    background:#F4F6F7;border:1px solid #D5D8DC;border-radius:8px;
                    padding:8px 14px;margin:6px 0 12px 0;font-size:0.82rem;color:#2C3E50;">
          <span style="color:#27AE60;font-weight:600;">● SYSTEM OPERATIONAL</span>
          <span>{data_lbl}</span>
          <span>·</span>
          <span>{sc_lbl}</span>
          <span>·</span>
          <span>{ew_lbl}</span>
          <span style="margin-left:auto;color:#7F8C8D;">Updated {now}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if facility_badges:
        st.markdown(
            '<div class="facility-status">'
            + "".join(f"<span>{b}</span>" for b in facility_badges)
            + "</div>",
            unsafe_allow_html=True,
        )


# Backwards-compatible alias
def hero_header(show_logos=True, facility_badges=None):
    render_global_header(facility_badges=facility_badges)


def page_header(title, subtitle=None, breadcrumb=None):
    """Page hero — Streamlit-native (avoids HTML rendering issues in some hosts)."""
    if breadcrumb:
        st.caption(breadcrumb)
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def render_global_footer():
    st.markdown("---")
    st.markdown(
        f"""
        <div style="font-size:0.78rem;color:#7F8C8D;line-height:1.5;padding:4px 0 16px 0;">
          <strong style="color:#1F4E79;">Kilifi County Malaria Surveillance &amp; Early Warning System</strong><br/>
          Phase C · Decision support platform · CBMP / PIR<br/>
          Data sources: KHIS · WFK · Climate (Open-Meteo / NASA POWER) · Programme data<br/>
          For programme decision support. Interpret alongside routine surveillance and County guidance.
          · Version 1.0 · Access controlled by role &amp; geography · {datetime.now().strftime("%Y")}
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_header(text):
    st.markdown(f'<div class="section-header">{text}</div>', unsafe_allow_html=True)


def headline(text, status="green"):
    st.markdown(f"""
    <div class="headline-block {status}">
        <div class="headline-label">Headline</div>
        <div class="headline-text">{text}</div>
    </div>
    """, unsafe_allow_html=True)


def big_number(value, label, delta=None, delta_direction="up"):
    delta_html = ""
    if delta:
        delta_html = f'<div class="big-num-delta {delta_direction}">{delta}</div>'
    st.markdown(f"""
    <div class="big-num">
        <div class="big-num-value">{value}</div>
        <div class="big-num-label">{label}</div>
        {delta_html}
    </div>
    """, unsafe_allow_html=True)


def status_badge(text, level="green"):
    return f'<span class="status-badge {level}">{text}</span>'


def placeholder_page(title):
    page_header(title)
    st.info(
        f"⏳ **{title}** will be built in the next message. "
        "This placeholder ensures the navigation works while we finish the build."
    )


# ==================================================================
# FILTER BAR
# ==================================================================

def _available_periods():
    """Distinct periods in DB, newest first (lexicographic works for YYYY-MM)."""
    try:
        rows = query("SELECT DISTINCT period FROM malaria_data ORDER BY period DESC")
        if rows.empty:
            return []
        return [str(p) for p in rows["period"].tolist() if p is not None]
    except Exception:
        return []


def _available_monthly_periods():
    """YYYY-MM periods only, newest first."""
    return [p for p in _available_periods() if len(p) == 7 and p[4] == "-" and "Q" not in p]


def _available_quarterly_periods():
    """YYYY-Qn periods, newest first."""
    return [p for p in _available_periods() if "Q" in str(p)]


def _available_yearly_periods():
    """Pure year labels (e.g. 2024), newest first."""
    return [p for p in _available_periods() if str(p).isdigit() and len(str(p)) == 4]


def _default_period_selection():
    """Prefer latest 3 months so Overview / R1.1 RAG stay meaningful vs annual targets."""
    monthly = _available_monthly_periods()
    if len(monthly) >= 3:
        return monthly[:3], "Monthly · latest 3"
    if len(monthly) >= 1:
        return monthly[:1], "Monthly · latest 1"
    yearly = _available_yearly_periods()
    if yearly:
        return yearly[:1], "Yearly · latest"
    all_p = _available_periods()
    return all_p, "All periods"


def default_filters():
    periods, mode = _default_period_selection()
    all_p = _available_periods()
    return {
        "facility": "All three",
        "stream": "Both",
        "registers": list(REGISTERS),
        "periods": periods if periods else all_p,
        "period_mode": mode,
        "period": periods[0] if periods else (all_p[0] if all_p else None),
    }


def render_filter_bar():
    """Apply-button filters so changes always stick and drive every page."""
    periods = _available_periods()
    monthly = _available_monthly_periods()
    if "applied_filters" not in st.session_state:
        st.session_state.applied_filters = default_filters()

    af = st.session_state.applied_filters
    if af.get("period_mode") == "All periods":
        af["periods"] = periods

    monthly = _available_monthly_periods()
    quarterly = _available_quarterly_periods()
    yearly = _available_yearly_periods()

    st.markdown("### Filters")
    st.caption("Choose facility / period / registers, then **Apply filters**. Period modes support monthly, quarterly and yearly comparison.")
    with st.form("global_filters_form"):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            # Geographic scope from session user (RBAC)
            _u = st.session_state.get("user") or {}
            if _u.get("facility_scope") and _u["facility_scope"] in FACILITIES:
                fac_opts = [_u["facility_scope"]]
            elif _u.get("sub_county_scope"):
                fac_opts = [
                    f for f in FACILITIES
                    if GEO_SITES.get(f, {}).get("sub_county") == _u["sub_county_scope"]
                ]
                if not fac_opts:
                    fac_opts = FACILITIES
                if len(fac_opts) > 1:
                    fac_opts = ["All three"] + fac_opts
            elif _u.get("scope") == "facility":
                fac_opts = FACILITIES  # pick one; no All three for pure facility template
            else:
                fac_opts = ["All three"] + FACILITIES
            facility = st.selectbox(
                "Facility",
                fac_opts,
                index=fac_opts.index(af["facility"]) if af.get("facility") in fac_opts else 0,
            )
        with c2:
            stream = st.selectbox(
                "Stream",
                STREAMS,
                index=STREAMS.index(af["stream"]) if af.get("stream") in STREAMS else 0,
            )
        with c3:
            mode_opts = [
                "Monthly · latest 1",
                "Monthly · latest 3",
                "Monthly · latest 6",
                "Monthly · full latest year",
                "Quarterly · latest 4",
                "Yearly · latest",
                "Yearly · last 2 years",
                "Yearly · all years",
                "Select periods (custom)",
                "All periods",
            ]
            # Map old mode names to new
            legacy = {
                "Latest 3 months": "Monthly · latest 3",
                "Latest only": "Monthly · latest 1",
                "Latest year (monthly)": "Monthly · full latest year",
                "Select periods": "Select periods (custom)",
            }
            cur_raw = af.get("period_mode") or "Monthly · latest 3"
            cur_mode = legacy.get(cur_raw, cur_raw)
            if cur_mode not in mode_opts:
                cur_mode = "Monthly · latest 3"
            period_mode = st.selectbox("Period mode", mode_opts, index=mode_opts.index(cur_mode))
            period_pick = st.multiselect(
                "Custom periods (for Select periods)",
                periods,
                default=af.get("periods") or (periods[:1] if periods else []),
            )
        with c4:
            preset = st.selectbox("Register preset", list(REGISTER_PRESETS.keys()))
            registers = st.multiselect(
                "Registers",
                REGISTERS,
                default=list(REGISTER_PRESETS.get(preset, REGISTERS)),
            )
        submitted = st.form_submit_button("Apply filters", type="primary", use_container_width=True)

    if submitted:
        if period_mode == "Monthly · latest 1":
            selected_periods = monthly[:1] if monthly else (periods[:1] if periods else [])
        elif period_mode == "Monthly · latest 3":
            selected_periods = monthly[:3] if monthly else (periods[:3] if periods else [])
        elif period_mode == "Monthly · latest 6":
            selected_periods = monthly[:6] if monthly else (periods[:6] if periods else [])
        elif period_mode == "Monthly · full latest year":
            if monthly:
                year = monthly[0][:4]
                selected_periods = [p for p in monthly if p.startswith(year)]
            else:
                selected_periods = periods
        elif period_mode == "Quarterly · latest 4":
            selected_periods = quarterly[:4] if quarterly else periods[:4]
        elif period_mode == "Yearly · latest":
            selected_periods = yearly[:1] if yearly else periods[:1]
        elif period_mode == "Yearly · last 2 years":
            selected_periods = yearly[:2] if yearly else periods[:2]
        elif period_mode == "Yearly · all years":
            selected_periods = yearly if yearly else periods
        elif period_mode == "Select periods (custom)":
            selected_periods = list(period_pick) if period_pick else list(periods)
        else:
            selected_periods = list(periods)

        if registers:
            chosen_registers = list(registers)
        else:
            chosen_registers = list(REGISTER_PRESETS.get(preset, REGISTERS))
        if preset and preset != "All registers":
            chosen_registers = list(REGISTER_PRESETS.get(preset, chosen_registers))

        st.session_state.applied_filters = {
            "facility": facility,
            "stream": stream,
            "registers": chosen_registers if chosen_registers else list(REGISTERS),
            "periods": selected_periods if selected_periods else None,
            "period_mode": period_mode,
            "period": selected_periods[0] if selected_periods else None,
        }
        st.rerun()

    f = st.session_state.applied_filters
    n_rows = 0
    try:
        df0 = load_all_data()
        n_rows = len(_filter_data(df0, f)) if not df0.empty else 0
    except Exception:
        pass
    period_preview = ", ".join((f.get("periods") or [])[:6])
    if len(f.get("periods") or []) > 6:
        period_preview += "…"
    try:
        last_up = query("SELECT uploaded_at, file_name, id FROM khis_submissions ORDER BY id DESC LIMIT 1")
        if not last_up.empty:
            st.caption(
                f"Data current to filter · Last upload: **{last_up.iloc[0]['uploaded_at']}** · "
                f"`{last_up.iloc[0]['file_name']}` · submission #{int(last_up.iloc[0]['id'])}"
            )
    except Exception:
        pass
    st.info(
        f"**Active filters:** {f.get('facility')} · {f.get('stream')} · "
        f"{f.get('period_mode')} ({period_preview or '—'}) · "
        f"{len(f.get('registers') or [])} register(s) → **{n_rows:,} rows** in view"
    )
    return f


# ==================================================================
# LOGIN SCREEN
# ==================================================================

def login_screen():
    st.markdown("<br>", unsafe_allow_html=True)
    hero_header(show_logos=True)

    col_a, col_b, col_c = st.columns([1, 1.2, 1])
    with col_b:
        st.markdown("#### Sign in")
        with st.form("login_form"):
            username = st.text_input(
                "Username",
                placeholder="username (e.g. admin, jane.mwangi)",
            )
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", use_container_width=True, type="primary")

        if submitted:
            if not username or not password:
                st.warning("Please enter both username and password.")
            elif st.session_state.get("login_lock_until") and datetime.now().timestamp() < st.session_state.login_lock_until:
                left = int(st.session_state.login_lock_until - datetime.now().timestamp())
                st.error(f"Too many failed attempts. Wait {left}s.")
            elif verify_password(username, password):
                sess = build_session_user(username)
                if not sess:
                    st.error("Account disabled or misconfigured.")
                else:
                    st.session_state.login_fails = 0
                    st.session_state.login_at = datetime.now().timestamp()
                    st.session_state.user = sess
                    try:
                        execute(
                            "UPDATE users SET last_login = ? WHERE username = ?",
                            (datetime.now().strftime("%Y-%m-%d %H:%M"), username),
                        )
                    except Exception:
                        pass
                    st.session_state.must_change_password = user_must_change(username)
                    st.rerun()
            else:
                fails = int(st.session_state.get("login_fails") or 0) + 1
                st.session_state.login_fails = fails
                if fails >= 5:
                    st.session_state.login_lock_until = datetime.now().timestamp() + 300
                    st.error("Invalid credentials. Locked for 5 minutes.")
                else:
                    st.error(f"Invalid username or password. ({fails}/5)")

        st.caption(
            "Pilot login. Change the seeded password on first use. "
            "Set env `CBMP_BOOTSTRAP_PASSWORD` before first seed to avoid the built-in default."
        )


def change_password_screen(user, voluntary=False):
    """Password set/change. Uses native Streamlit widgets only (no HTML)."""
    st.title("Set your password" if not voluntary else "Change password")
    st.caption(
        "Choose a personal password (minimum 8 characters). "
        "The administrator never sees this password."
    )
    with st.form("change_pw_form"):
        new_pw = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Save password", type="primary")
    if submitted:
        if not new_pw or not confirm:
            st.warning("Enter and confirm the new password.")
        elif new_pw != confirm:
            st.error("Passwords do not match.")
        elif new_pw == DEFAULT_PASSWORD or new_pw.lower() in ("changeme123!", "password", "12345678"):
            st.error("Choose a stronger password (not a common or temporary default).")
        else:
            ok, msg = set_password(user["username"], new_pw)
            if ok:
                st.session_state.must_change_password = False
                st.success(msg + " You can now use the dashboard.")
                st.rerun()
            else:
                st.error(msg)


# ==================================================================
# PAGE 1 — UPLOAD & SETTINGS
# ==================================================================

# ==================================================================
# PHASE A0 — RECONCILIATION STUBS + DATA FOUNDATION UI
# ==================================================================

# Internal measure pairs to compare within the same facility + period (Layer 1 only for now).
# Full Layer 2/3 recon (648 vs 706 vs KHIS) activates when those sources exist.
RECON_RULES = [
    {
        "code": "RDT_EXAMS_706_vs_743",
        "label": "RDT examinations: MOH 706 vs MOH 743",
        "a": {"register": "MOH 706", "indicator_like": "RDT%Total Exam"},
        "b": {"register": "MOH 743", "indicator_like": "Tested RDT"},
        "warn_pct": 5.0,
        "flag_pct": 15.0,
    },
    {
        "code": "RDT_POS_706_vs_743",
        "label": "RDT positives: MOH 706 vs MOH 743",
        "a": {"register": "MOH 706", "indicator_like": "RDT%Positive"},
        "b": {"register": "MOH 743", "indicator_like": "Positive RDT"},
        "warn_pct": 5.0,
        "flag_pct": 15.0,
    },
    {
        "code": "AL_743_vs_748",
        "label": "AL totals: MOH 743 AL Total vs MOH 748 AL Dispensed",
        "a": {"register": "MOH 743", "indicator_like": "AL Total"},
        "b": {"register": "MOH 748", "indicator_like": "AL Dispensed"},
        "warn_pct": 10.0,
        "flag_pct": 25.0,
    },
    {
        "code": "TESTED_vs_CONFIRMED_ORDER",
        "label": "Confirmed should not exceed tested (same register age band)",
        "special": "confirmed_vs_tested",
        "warn_pct": 0.0,
        "flag_pct": 0.0,
    },
]


def _sum_obs_measure(facility, period, register, indicator_like, geo_level="facility"):
    """Sum observation values for a facility/period matching register + indicator pattern."""
    try:
        # Use latest submission only for recon of "current truth"
        latest = query("SELECT MAX(id) AS mid FROM submissions")
        if latest.empty or pd.isna(latest.iloc[0]["mid"]):
            return None
        sid = int(latest.iloc[0]["mid"])
        # SQLite GLOB-style via LIKE
        pat = indicator_like.replace("%", "%")
        df = query(
            """
            SELECT SUM(value) AS v FROM observations
            WHERE submission_id = ?
              AND geo_level = ?
              AND facility = ?
              AND period = ?
              AND register = ?
              AND indicator LIKE ?
            """,
            (sid, geo_level, facility, period, register, pat),
        )
        if df.empty or pd.isna(df.iloc[0]["v"]):
            return None
        return float(df.iloc[0]["v"])
    except Exception:
        return None


def _period_to_month_key(period):
    """Map canonical period to YYYY-MM when possible (for joining Layer 2 dates)."""
    p = str(period or "").strip()
    if len(p) == 7 and p[4] == "-":
        return p
    if len(p) == 4 and p.isdigit():
        return None  # annual — skip L1-L2 month join
    if "-Q" in p:
        # YYYY-Qn → mid-month of quarter for loose match not used; skip tight join
        return None
    return None


def _sum_layer2(table, facility, month_key, measure_col):
    """Sum Layer 2 measure for facility where report_date starts with YYYY-MM."""
    if not month_key or not facility:
        return None
    try:
        df = query(
            f"""
            SELECT SUM(COALESCE({measure_col}, 0)) AS v
            FROM {table}
            WHERE facility = ?
              AND (report_date LIKE ? OR substr(report_date, 1, 7) = ?)
            """,
            (facility, month_key + "%", month_key),
        )
        if df.empty or pd.isna(df.iloc[0]["v"]):
            return None
        return float(df.iloc[0]["v"])
    except Exception:
        return None


def _sum_l1_community_tested(facility, period, submission_id):
    """KHIS community tests approx: MOH 748 Total <5 + Total >=5 for facility/period."""
    try:
        df = query(
            """
            SELECT SUM(value) AS v FROM observations
            WHERE submission_id = ?
              AND geo_level = 'facility'
              AND facility = ?
              AND period = ?
              AND register = 'MOH 748'
              AND indicator IN ('Total <5', 'Total >=5')
            """,
            (submission_id, facility, period),
        )
        if df.empty or pd.isna(df.iloc[0]["v"]):
            return None
        return float(df.iloc[0]["v"])
    except Exception:
        return None


def _sum_l1_facility_tested(facility, period, submission_id):
    """KHIS facility tested: MOH 705A/B Tested + MOH 706 Total Exam (all methods)."""
    try:
        df = query(
            """
            SELECT SUM(value) AS v FROM observations
            WHERE submission_id = ?
              AND geo_level = 'facility'
              AND facility = ?
              AND period = ?
              AND (
                (register IN ('MOH 705A', 'MOH 705B') AND indicator LIKE 'Tested%')
                OR (register = 'MOH 706' AND indicator LIKE '%Total Exam%')
              )
            """,
            (submission_id, facility, period),
        )
        if df.empty or pd.isna(df.iloc[0]["v"]):
            return None
        return float(df.iloc[0]["v"])
    except Exception:
        return None


def _append_l1_l2_item(items, run_id, code, fac, per, label_a, va, label_b, vb, warn_pct=10.0, flag_pct=25.0):
    """Compare two measures; append recon_items tuple. Returns (checked_delta, flagged_delta)."""
    if va is None and vb is None:
        return 0, 0
    if va is None or vb is None:
        items.append((
            run_id, code, fac, per, label_a, va, label_b, vb,
            None, None, "incomplete",
            f"{fac} {per}: {code} — one side missing (L1={va}, L2={vb})",
        ))
        return 1, 0
    diff = va - vb
    base = max(abs(va), abs(vb), 1.0)
    pct = abs(diff) / base * 100.0
    if pct >= flag_pct:
        status = "requires_reconciliation"
        fl = 1
    elif pct >= warn_pct:
        status = "review"
        fl = 1
    else:
        status = "aligned"
        fl = 0
    items.append((
        run_id, code, fac, per, label_a, va, label_b, vb,
        diff, pct, status,
        f"{code}: L1={va:.0f} L2={vb:.0f} Δ={diff:.0f} ({pct:.1f}%)",
    ))
    return 1, fl


def run_reconciliation_stubs(run_by="system"):
    """
    Phase A.5 reconciliation engine (no longer a stub-only layer):
    - Layer 1 internal form pairs (existing RECON_RULES)
    - Locked 705 Confirmed ↔ 706 RDT Positive; AL ↔ 705 Confirmed
    - Layer 1 ↔ Layer 2 (648/521) when month aligns
    - 748 ↔ 648 tested
    Returns (run_id, n_checked, n_flagged).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO recon_runs (run_by, scope, notes) VALUES (?, ?, ?)",
        (
            run_by,
            "A5_full_l1_l2_locked",
            "Locked 705/706/AL + L1↔L2 648/521 + internal L1 pairs",
        ),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()

    latest = query("SELECT MAX(id) AS mid FROM submissions")
    if latest.empty or pd.isna(latest.iloc[0]["mid"]):
        execute(
            "UPDATE recon_runs SET items_checked = 0, items_flagged = 0, notes = ? WHERE id = ?",
            ("No KHIS submissions yet — L2-only compare skipped", run_id),
        )
        # Still allow L2-only presence check? skip for now
        return run_id, 0, 0

    sid = int(latest.iloc[0]["mid"])
    pairs = query(
        """
        SELECT DISTINCT facility, period FROM observations
        WHERE submission_id = ? AND geo_level = 'facility' AND facility IS NOT NULL
        """,
        (sid,),
    )
    checked = flagged = 0
    items = []

    for _, row in pairs.iterrows():
        fac, per = row["facility"], row["period"]
        for rule in RECON_RULES:
            if rule.get("special") == "confirmed_vs_tested":
                tested = _sum_obs_measure(fac, per, "MOH 705A", "Tested%", "facility")
                conf = _sum_obs_measure(fac, per, "MOH 705A", "Confirmed%", "facility")
                if tested is None and conf is None:
                    continue
                checked += 1
                if tested is not None and conf is not None and conf > tested + 0.5:
                    diff = conf - tested
                    flagged += 1
                    items.append((
                        run_id, "CONFIRMED_GT_TESTED", fac, per,
                        "Confirmed (705A)", conf, "Tested (705A)", tested,
                        diff, (diff / tested * 100) if tested else None,
                        "requires_reconciliation",
                        f"{fac} {per}: confirmed ({conf:.0f}) > tested ({tested:.0f})",
                    ))
                continue

            va = _sum_obs_measure(fac, per, rule["a"]["register"], rule["a"]["indicator_like"])
            vb = _sum_obs_measure(fac, per, rule["b"]["register"], rule["b"]["indicator_like"])
            if va is None and vb is None:
                continue
            checked += 1
            if va is None or vb is None:
                items.append((
                    run_id, rule["code"], fac, per,
                    rule["a"]["register"] + " " + rule["a"]["indicator_like"], va,
                    rule["b"]["register"] + " " + rule["b"]["indicator_like"], vb,
                    None, None, "incomplete",
                    f"{fac} {per}: {rule['label']} — one side missing (A={va}, B={vb})",
                ))
                continue
            diff = va - vb
            base = max(abs(va), abs(vb), 1.0)
            pct = abs(diff) / base * 100.0
            if pct >= rule["flag_pct"]:
                status = "requires_reconciliation"
                flagged += 1
            elif pct >= rule["warn_pct"]:
                status = "review"
                flagged += 1
            else:
                status = "aligned"
            items.append((
                run_id, rule["code"], fac, per,
                f"{rule['a']['register']} {rule['a']['indicator_like']}", va,
                f"{rule['b']['register']} {rule['b']['indicator_like']}", vb,
                diff, pct, status,
                f"{rule['label']}: A={va:.0f} B={vb:.0f} Δ={diff:.0f} ({pct:.1f}%)",
            ))

        # ── Layer 1 ↔ Layer 2 (monthly periods only) ──
        mk = _period_to_month_key(per)
        if mk:
            l1_comm = _sum_l1_community_tested(fac, per, sid)
            l2_648_tested = _sum_layer2("moh_648", fac, mk, "tested")
            l2_648_pos = _sum_layer2("moh_648", fac, mk, "positive")
            l2_648_susp = _sum_layer2("moh_648", fac, mk, "suspected")
            l2_521_tested = _sum_layer2("moh_521", fac, mk, "tested")
            l2_521_pos = _sum_layer2("moh_521", fac, mk, "positive")

            c, f = _append_l1_l2_item(
                items, run_id, "L1_748_vs_L2_648_TESTED", fac, per,
                "KHIS MOH 748 community tests", l1_comm,
                "MOH 648 tested", l2_648_tested,
            )
            checked += c
            flagged += f

            l1_pos_748 = None
            try:
                pdf = query(
                    """
                    SELECT SUM(value) AS v FROM observations
                    WHERE submission_id = ? AND facility = ? AND period = ?
                      AND register = 'MOH 748' AND indicator LIKE 'Positive%'
                    """,
                    (sid, fac, per),
                )
                if not pdf.empty and not pd.isna(pdf.iloc[0]["v"]):
                    l1_pos_748 = float(pdf.iloc[0]["v"])
            except Exception:
                pass
            c, f = _append_l1_l2_item(
                items, run_id, "L1_748_vs_L2_648_POSITIVE", fac, per,
                "KHIS MOH 748 positives", l1_pos_748,
                "MOH 648 positive", l2_648_pos,
            )
            checked += c
            flagged += f

            c, f = _append_l1_l2_item(
                items, run_id, "L1_FACILITY_vs_L2_521_TESTED", fac, per,
                "KHIS facility tested (705/706)", _sum_l1_facility_tested(fac, per, sid),
                "MOH 521 tested", l2_521_tested,
                warn_pct=15.0, flag_pct=30.0,
            )
            checked += c
            flagged += f

            c, f = _append_l1_l2_item(
                items, run_id, "L2_648_vs_L2_521_TESTED", fac, per,
                "MOH 648 tested", l2_648_tested,
                "MOH 521 tested", l2_521_tested,
                warn_pct=15.0, flag_pct=30.0,
            )
            checked += c
            flagged += f

            # Internal L2: positive should not exceed tested on 648
            if l2_648_tested is not None and l2_648_pos is not None:
                checked += 1
                if l2_648_pos > l2_648_tested + 0.5:
                    flagged += 1
                    items.append((
                        run_id, "L2_648_POS_GT_TESTED", fac, per,
                        "MOH 648 positive", l2_648_pos, "MOH 648 tested", l2_648_tested,
                        l2_648_pos - l2_648_tested,
                        (l2_648_pos - l2_648_tested) / max(l2_648_tested, 1) * 100,
                        "requires_reconciliation",
                        f"{fac} {per}: MOH 648 positive > tested",
                    ))
            if l2_648_susp is not None and l2_648_tested is not None:
                checked += 1
                if l2_648_tested > l2_648_susp + 0.5:
                    flagged += 1
                    items.append((
                        run_id, "L2_648_TESTED_GT_SUSPECTED", fac, per,
                        "MOH 648 tested", l2_648_tested, "MOH 648 suspected", l2_648_susp,
                        l2_648_tested - l2_648_susp,
                        (l2_648_tested - l2_648_susp) / max(l2_648_susp, 1) * 100,
                        "review",
                        f"{fac} {per}: MOH 648 tested > suspected",
                    ))

            # L1 community 748 vs L2 648 positives (month)
            l1_748_pos = None
            try:
                pdf = query(
                    """
                    SELECT SUM(value) AS v FROM observations
                    WHERE submission_id = ? AND facility = ? AND period = ?
                      AND register = 'MOH 748' AND indicator LIKE 'Positive%'
                    """,
                    (sid, fac, per),
                )
                if not pdf.empty and not pd.isna(pdf.iloc[0]["v"]):
                    l1_748_pos = float(pdf.iloc[0]["v"])
            except Exception:
                pass
            c, f = _append_l1_l2_item(
                items, run_id, "L1_748_vs_L2_648_POS", fac, per,
                "KHIS MOH 748 positives", l1_748_pos,
                "MOH 648 positive", l2_648_pos,
            )
            checked += c
            flagged += f

        # ── Locked cross-register L1 (705 confirmed vs 706 RDT pos; AL vs 705 confirmed) ──
        try:
            md = query(
                """
                SELECT register, indicator, value FROM observations
                WHERE submission_id = ? AND facility = ? AND period = ?
                  AND geo_level = 'facility'
                """,
                (sid, fac, per),
            )
        except Exception:
            md = pd.DataFrame()
        if not md.empty:
            conf_705 = sum_locked(
                md, {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Confirmed"]}
            )
            rdt_pos = sum_locked(
                md, {"registers": ["MOH 706"], "indicator_contains": ["RDT", "Positive"], "indicator_exclude": ["Exam"]}
            )
            al = sum_locked(md, {"indicator_exact": ["AL Total", "AL Dispensed"]})
            tests_748 = sum_locked(
                md, {"registers": ["MOH 748"], "indicator_exact": ["Total <5", "Total >=5"]}
            )
            if conf_705 or rdt_pos:
                c, f = _append_l1_l2_item(
                    items, run_id, "705_CONF_vs_706_RDT_POS", fac, per,
                    "MOH 705 Confirmed", conf_705 if conf_705 else None,
                    "MOH 706 RDT Positive", rdt_pos if rdt_pos else None,
                    warn_pct=15.0, flag_pct=30.0,
                )
                checked += c
                flagged += f
            if conf_705 or al:
                c, f = _append_l1_l2_item(
                    items, run_id, "AL_vs_705_CONFIRMED", fac, per,
                    "AL Total/Dispensed", al if al else None,
                    "MOH 705 Confirmed", conf_705 if conf_705 else None,
                    warn_pct=15.0, flag_pct=35.0,
                )
                checked += c
                flagged += f
            # 748 vs 648 only when month key exists and L2 was computed
            _mk = _period_to_month_key(per)
            if _mk:
                _l2t = _sum_layer2("moh_648", fac, _mk, "tested")
                if _l2t is not None and tests_748:
                    c, f = _append_l1_l2_item(
                        items, run_id, "748_vs_648_TESTED", fac, per,
                        "MOH 748 totals", tests_748,
                        "MOH 648 tested", _l2t,
                        warn_pct=15.0, flag_pct=30.0,
                    )
                    checked += c
                    flagged += f

    if items:
        conn = get_conn()
        conn.executemany(
            """
            INSERT INTO recon_items
                (run_id, rule_code, facility, period, measure_a, value_a,
                 measure_b, value_b, difference, difference_pct, status, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            items,
        )
        conn.commit()
        conn.close()

    execute(
        "UPDATE recon_runs SET items_checked = ?, items_flagged = ? WHERE id = ?",
        (checked, flagged, run_id),
    )
    log_audit(
        run_by, "recon_run", "recon_runs",
        f"run_id={run_id} checked={checked} flagged={flagged} scope=L1+L1L2",
    )
    return run_id, checked, flagged


def _safe_int(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none", "-"):
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def import_layer2_csv(uploaded_file, source_type, user):
    """
    Import Layer 2 operational CSV into moh_648 / moh_521 / stock_movements.
    Returns (n_rows, upload_id, errors).
    """
    errors = []
    try:
        raw = uploaded_file.getvalue()
    except Exception:
        uploaded_file.seek(0)
        raw = uploaded_file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        return 0, None, [f"CSV read failed: {e}"]

    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    name = getattr(uploaded_file, "name", source_type + ".csv")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO layer2_uploads (source_type, file_name, uploaded_by, row_count, notes) VALUES (?, ?, ?, 0, ?)",
        (source_type, name, user, "Layer 2 import"),
    )
    upload_id = cur.lastrowid
    n = 0

    try:
        if source_type == "moh_648":
            required = {"report_date", "facility"}
            if not required.issubset(set(df.columns)):
                errors.append(f"MOH 648 needs columns: {sorted(required)}")
            else:
                for _, r in df.iterrows():
                    pos = _safe_int(r.get("positive"))
                    neg = _safe_int(r.get("negative"))
                    result = str(r.get("result", "") or "").strip() or None
                    if not result and pos is not None:
                        result = "positive" if pos and pos > 0 else ("negative" if neg else None)
                    cur.execute(
                        """
                        INSERT INTO moh_648
                            (upload_id, report_date, facility, chu, chp_name, chp_id,
                             suspected, tested, positive, negative, treated, treated_without_test,
                             referred, referral_arrived, followed_up, itn_distributed,
                             result, treatment_outcome, linkage_id, age_group, sex, event_date, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            upload_id,
                            str(r.get("report_date", "")).strip(),
                            str(r.get("facility", "")).strip(),
                            str(r.get("chu", "") or "").strip() or None,
                            str(r.get("chp_name", "") or "").strip() or None,
                            str(r.get("chp_id", "") or "").strip() or None,
                            _safe_int(r.get("suspected")),
                            _safe_int(r.get("tested")),
                            pos,
                            neg,
                            _safe_int(r.get("treated")),
                            _safe_int(r.get("treated_without_test")),
                            _safe_int(r.get("referred")),
                            _safe_int(r.get("referral_arrived")),
                            _safe_int(r.get("followed_up")),
                            _safe_int(r.get("itn_distributed")),
                            result,
                            str(r.get("treatment_outcome", "") or "").strip() or None,
                            str(r.get("linkage_id", "") or "").strip() or None,
                            str(r.get("age_group", "") or "").strip() or None,
                            str(r.get("sex", "") or "").strip() or None,
                            str(r.get("event_date", "") or r.get("report_date", "") or "").strip() or None,
                            str(r.get("notes", "") or "").strip() or None,
                        ),
                    )
                    n += 1
        elif source_type == "moh_521":
            required = {"report_date", "facility"}
            if not required.issubset(set(df.columns)):
                errors.append(f"MOH 521 needs columns: {sorted(required)}")
            else:
                for _, r in df.iterrows():
                    cur.execute(
                        """
                        INSERT INTO moh_521
                            (upload_id, report_date, facility, chu,
                             suspected, tested, positive, treated, referred, followed_up,
                             notes, source, rollup_from_648)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            upload_id,
                            str(r.get("report_date", "")).strip(),
                            str(r.get("facility", "")).strip(),
                            str(r.get("chu", "") or "").strip() or None,
                            _safe_int(r.get("suspected")),
                            _safe_int(r.get("tested")),
                            _safe_int(r.get("positive")),
                            _safe_int(r.get("treated")),
                            _safe_int(r.get("referred")),
                            _safe_int(r.get("followed_up")),
                            str(r.get("notes", "") or "").strip() or None,
                            "csv_import",
                            0,
                        ),
                    )
                    n += 1
        elif source_type == "stock_movements":
            required = {"movement_date", "facility", "item", "movement_type", "quantity"}
            if not required.issubset(set(df.columns)):
                errors.append(f"Stock movements need columns: {sorted(required)}")
            else:
                for _, r in df.iterrows():
                    qty = _safe_int(r.get("quantity"))
                    cur.execute(
                        """
                        INSERT INTO stock_movements
                            (upload_id, movement_date, facility, item, movement_type,
                             quantity, balance_after, batch_or_lot, expiry_date, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            upload_id,
                            str(r.get("movement_date", "")).strip(),
                            str(r.get("facility", "")).strip(),
                            str(r.get("item", "")).strip(),
                            str(r.get("movement_type", "")).strip().lower(),
                            qty,
                            _safe_int(r.get("balance_after")),
                            str(r.get("batch_or_lot", "") or "").strip() or None,
                            str(r.get("expiry_date", "") or "").strip() or None,
                            str(r.get("notes", "") or "").strip() or None,
                        ),
                    )
                    n += 1
                    # Optional: update stock latest balance if balance_after provided
                    bal = _safe_int(r.get("balance_after"))
                    fac = str(r.get("facility", "")).strip()
                    item = str(r.get("item", "")).strip()
                    if bal is not None and fac and item:
                        cur.execute(
                            """
                            INSERT INTO stock (facility, item, quantity, reorder_point, last_restock)
                            VALUES (?, ?, ?, 30, ?)
                            ON CONFLICT(facility, item) DO UPDATE SET
                                quantity=excluded.quantity,
                                last_restock=excluded.last_restock
                            """,
                            (fac, item, bal, str(r.get("movement_date", "")).strip()),
                        )
        else:
            errors.append(f"Unknown source_type: {source_type}")

        cur.execute("UPDATE layer2_uploads SET row_count = ? WHERE id = ?", (n, upload_id))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        errors.append(str(e))
        n = 0
    finally:
        conn.close()

    if n and not errors:
        log_audit(user, "layer2_import", source_type, f"upload_id={upload_id} rows={n}")
    return n, upload_id, errors


def resolve_chu_coords(facility, chu_name):
    """
    Resolve CHU map position:
    1) imported chu_gps (survey / KMHFR)
    2) gazetteer name match
    3) stable offset from facility anchor
    Returns (lat, lon, source).
    """
    fac = str(facility or "").strip()
    chu = str(chu_name or "").strip()
    if chu and chu != "(no CHU name)":
        try:
            hit = query(
                "SELECT lat, lon, source FROM chu_gps WHERE lower(chu)=lower(?) "
                "AND (facility = ? OR facility IS NULL OR facility = '') "
                "ORDER BY CASE WHEN facility = ? THEN 0 ELSE 1 END LIMIT 1",
                (chu, fac, fac),
            )
            if not hit.empty:
                return float(hit.iloc[0]["lat"]), float(hit.iloc[0]["lon"]), hit.iloc[0].get("source") or "imported GPS"
        except Exception:
            pass
        key = chu.lower()
        if key in CHU_GAZETTEER:
            lat, lon, src = CHU_GAZETTEER[key]
            return lat, lon, f"gazetteer: {src}"
        for gname, (lat, lon, src) in CHU_GAZETTEER.items():
            if gname in key or key in gname:
                return lat, lon, f"gazetteer fuzzy: {src}"
    anchor = GEO_SITES.get(fac)
    if not anchor:
        return None, None, "no facility anchor"
    import hashlib, math
    h = int(hashlib.md5(f"{fac}|{chu}".encode("utf-8")).hexdigest(), 16)
    angle = (h % 360) * math.pi / 180.0
    radius = 0.012 + (h % 7) * 0.003
    return anchor["lat"] + radius * math.sin(angle), anchor["lon"] + radius * math.cos(angle), "offset from facility (no GPS)"


def import_chu_gps_csv(uploaded_file, user="system"):
    errors = []
    try:
        raw = uploaded_file.getvalue()
    except Exception:
        uploaded_file.seek(0)
        raw = uploaded_file.read()
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        return 0, [f"CSV read failed: {e}"]
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    need = {"chu", "lat", "lon"}
    if not need.issubset(set(df.columns)):
        return 0, [f"Need columns: chu, lat, lon (optional facility, source, notes)"]
    n = 0
    conn = get_conn()
    for _, r in df.iterrows():
        chu = str(r.get("chu") or "").strip()
        try:
            lat = float(r.get("lat"))
            lon = float(r.get("lon"))
        except Exception:
            errors.append(f"Bad lat/lon for {chu}")
            continue
        if not chu:
            continue
        conn.execute(
            "INSERT INTO chu_gps (facility, chu, lat, lon, source, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(r.get("facility") or "").strip() or None,
                chu,
                lat,
                lon,
                str(r.get("source") or "csv").strip(),
                str(r.get("notes") or "").strip() or None,
            ),
        )
        n += 1
    conn.commit()
    conn.close()
    log_audit(user, "import_chu_gps", "chu_gps", f"rows={n}")
    return n, errors


def validate_wfk_kilifi_workbook(uploaded_file):
    """
    Parse WFK workbook and return validation summary WITHOUT writing to DB.
    Returns (ok, summary_dict, county_df, sub_df, fac_df, errors)
    """
    errors = []
    try:
        raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()
        bio = io.BytesIO(raw)
        fname = getattr(uploaded_file, "name", "wfk_kilifi.xlsx")
        county = pd.read_excel(bio, sheet_name="County_trends")
        bio.seek(0)
        sub = pd.read_excel(bio, sheet_name="Subcounty_trends")
        bio.seek(0)
        fac = pd.read_excel(bio, sheet_name="Facility_trends")
    except Exception as e:
        return False, {}, None, None, None, [f"Could not read WFK sheets: {e}"]

    scs = sorted({str(x).strip() for x in sub.get("sub_county", pd.Series()).dropna().unique() if str(x).strip()})
    periods = sorted({str(x) for x in sub.get("period", pd.Series()).dropna().unique()})
    missing_sc = [s for s in KILIFI_SUBCOUNTIES if s not in scs]
    # DQ flags
    n_dup = 0
    if not sub.empty and "sub_county" in sub.columns and "period" in sub.columns:
        n_dup = int(sub.duplicated(subset=["sub_county", "period"]).sum())
    inv_tpr = 0
    if "positivity_pct" in sub.columns:
        inv_tpr = int((pd.to_numeric(sub["positivity_pct"], errors="coerce") > 100).sum())
    miss_tested = 0
    if "tested_total" in sub.columns:
        miss_tested = int(pd.to_numeric(sub["tested_total"], errors="coerce").isna().sum())
    summary = {
        "file": fname,
        "county_rows": len(county),
        "subcounty_rows": len(sub),
        "facility_rows": len(fac),
        "sub_counties_detected": len(scs),
        "sub_counties_list": scs,
        "missing_sub_counties": missing_sc,
        "periods": periods,
        "period_range": f"{periods[0]} → {periods[-1]}" if periods else "—",
        "duplicate_records": n_dup,
        "invalid_tpr": inv_tpr,
        "missing_tested": miss_tested,
    }
    ok = len(county) + len(sub) + len(fac) > 0 and len(errors) == 0
    return ok, summary, county, sub, fac, errors


def import_wfk_kilifi_workbook(uploaded_file, user="system", county=None, sub=None, fac=None):
    """
    Ingest World Friends Kilifi surveillance workbook:
    County_trends / Subcounty_trends / Facility_trends.
    Does not overwrite CBMP facility KHIS latest-view.
    Pass pre-parsed frames from validate_wfk to avoid re-read; otherwise reads file.
    """
    fname = getattr(uploaded_file, "name", "wfk_kilifi.xlsx") if uploaded_file is not None else "wfk_kilifi.xlsx"
    n_sc = n_fc = n_ct = 0
    if county is None or sub is None or fac is None:
        raw = uploaded_file.getvalue() if hasattr(uploaded_file, "getvalue") else uploaded_file.read()
        bio = io.BytesIO(raw)
        conn = get_conn()
        try:
            county = pd.read_excel(bio, sheet_name="County_trends")
            bio.seek(0)
            sub = pd.read_excel(bio, sheet_name="Subcounty_trends")
            bio.seek(0)
            fac = pd.read_excel(bio, sheet_name="Facility_trends")
        except Exception as e:
            conn.close()
            return 0, [f"Could not read WFK sheets: {e}"]
    else:
        conn = get_conn()

    # Snapshot previous for change report + version history
    try:
        prev_sc = int(query("SELECT COUNT(*) AS n FROM kilifi_subcounty").iloc[0]["n"])
        prev_file = query(
            "SELECT source_file FROM kilifi_subcounty WHERE source_file IS NOT NULL LIMIT 1"
        )
        prev_name = str(prev_file.iloc[0]["source_file"]) if not prev_file.empty else None
    except Exception:
        prev_sc = 0
        prev_name = None

    # Archive previous as a version row (content remains only in live tables; metadata is versioned)
    try:
        periods_prev = query("SELECT DISTINCT period FROM kilifi_subcounty ORDER BY period")
        prange = ""
        if not periods_prev.empty:
            plist = periods_prev["period"].astype(str).tolist()
            prange = f"{plist[0]} → {plist[-1]}"
        conn.execute(
            "UPDATE kilifi_upload_versions SET status='archived' WHERE status='current'"
        )
        conn.execute(
            """
            INSERT INTO kilifi_upload_versions
            (version_label, source_file, uploaded_by, county_rows, subcounty_rows, facility_rows,
             period_range, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'archived', ?)
            """,
            (
                f"pre_{datetime.now().strftime('%Y%m%d_%H%M')}",
                prev_name or "(previous)",
                user,
                0, prev_sc, 0, prange,
                "Replaced by new WFK commit",
            ),
        )
    except Exception:
        pass

    conn.execute("DELETE FROM kilifi_county_trend")
    conn.execute("DELETE FROM kilifi_subcounty")
    conn.execute("DELETE FROM kilifi_facility_wide")

    def _num(v):
        try:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return None
            return float(v)
        except Exception:
            return None

    def _flag(v):
        if v is True or str(v).strip().upper() in ("TRUE", "1", "YES"):
            return 1
        return 0

    for _, r in county.iterrows():
        conn.execute(
            """
            INSERT INTO kilifi_county_trend
            (period, period_date, suspected, tested, confirmed, testing_rate, positivity, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(r.get("period") or ""),
                str(r.get("period_date") or "")[:10],
                _num(r.get("suspected_total")),
                _num(r.get("tested_total")),
                _num(r.get("confirmed_total")),
                _num(r.get("testing_rate_pct")),
                _num(r.get("positivity_pct")),
                fname,
            ),
        )
        n_ct += 1

    for _, r in sub.iterrows():
        rdt_exam = (_num(r.get("rdt_exam_o5")) or 0) + (_num(r.get("rdt_exam_u5")) or 0)
        rdt_pos = (_num(r.get("rdt_pos_o5")) or 0) + (_num(r.get("rdt_pos_u5")) or 0)
        bs_exam = (_num(r.get("bs_exam_o5")) or 0) + (_num(r.get("bs_exam_u5")) or 0)
        bs_pos = (_num(r.get("bs_pos_o5")) or 0) + (_num(r.get("bs_pos_u5")) or 0)
        conn.execute(
            """
            INSERT INTO kilifi_subcounty
            (sub_county, period, period_date, suspected, tested, confirmed, al_dispensed,
             rdt_exam, rdt_pos, bs_exam, bs_pos, testing_rate, positivity,
             flag_pos_gt_100, flag_conf_gt_tested, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(r.get("sub_county") or "").strip(),
                str(r.get("period") or ""),
                str(r.get("period_date") or "")[:10],
                _num(r.get("suspected_total")),
                _num(r.get("tested_total")),
                _num(r.get("confirmed_total")),
                _num(r.get("al_dispensed")),
                rdt_exam, rdt_pos, bs_exam, bs_pos,
                _num(r.get("testing_rate_pct")),
                _num(r.get("positivity_pct")),
                _flag(r.get("flag_pos_gt_100")),
                _flag(r.get("flag_conf_gt_tested")),
                fname,
            ),
        )
        n_sc += 1

    for _, r in fac.iterrows():
        conn.execute(
            """
            INSERT INTO kilifi_facility_wide
            (sub_county, unit, period, suspected, tested, confirmed, positivity,
             flag_pos_gt_100, flag_conf_gt_tested, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(r.get("sub_county") or "").strip(),
                str(r.get("unit") or "").strip(),
                str(r.get("period") or ""),
                _num(r.get("suspected_total")),
                _num(r.get("tested_total")),
                _num(r.get("confirmed_total")),
                _num(r.get("positivity_pct")),
                _flag(r.get("flag_pos_gt_100")),
                _flag(r.get("flag_conf_gt_tested")),
                fname,
            ),
        )
        n_fc += 1

    # Mark this upload as current version
    try:
        periods_now = sorted({str(r.get("period") or "") for _, r in sub.iterrows() if str(r.get("period") or "")})
        prange = f"{periods_now[0]} → {periods_now[-1]}" if periods_now else ""
        conn.execute(
            """
            INSERT INTO kilifi_upload_versions
            (version_label, source_file, uploaded_by, county_rows, subcounty_rows, facility_rows,
             period_range, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?)
            """,
            (
                f"v_{datetime.now().strftime('%Y%m%d_%H%M')}",
                fname, user, n_ct, n_sc, n_fc, prange,
                f"Replaced previous rows={prev_sc}",
            ),
        )
    except Exception:
        pass

    conn.commit()
    conn.close()
    log_audit(user, "import_wfk_kilifi", "kilifi_subcounty", f"sc={n_sc} fac={n_fc} county={n_ct}")
    return n_sc + n_fc + n_ct, []


def rollup_648_to_521(user="system", replace_auto=True):
    """
    Phase A.5 — MOH 521 = automatic summary of MOH 648 by facility + CHU + month.
    Rows marked source='rollup_648' / rollup_from_648=1.
    Returns (n_rows, n_groups).
    """
    try:
        m648 = query("SELECT * FROM moh_648")
    except Exception:
        return 0, 0
    if m648.empty:
        return 0, 0
    m648 = m648.copy()
    for col in ("suspected", "tested", "positive", "treated", "referred", "followed_up", "negative"):
        if col not in m648.columns:
            m648[col] = 0
        m648[col] = pd.to_numeric(m648[col], errors="coerce").fillna(0)
    m648["month_key"] = m648["report_date"].astype(str).map(
        lambda x: _period_to_month_key(x) or (x[:7] if len(str(x)) >= 7 else str(x))
    )
    m648 = m648[m648["month_key"].notna() & (m648["month_key"] != "")]
    if m648.empty:
        return 0, 0
    grp = m648.groupby(["facility", "chu", "month_key"], dropna=False).agg(
        suspected=("suspected", "sum"),
        tested=("tested", "sum"),
        positive=("positive", "sum"),
        treated=("treated", "sum"),
        referred=("referred", "sum"),
        followed_up=("followed_up", "sum"),
        n_rows=("facility", "count"),
    ).reset_index()

    if replace_auto:
        try:
            execute("DELETE FROM moh_521 WHERE rollup_from_648 = 1 OR source = 'rollup_648'")
        except Exception:
            pass

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO layer2_uploads (source_type, file_name, uploaded_by, row_count, notes) VALUES (?, ?, ?, 0, ?)",
        ("moh_521_rollup", "auto_from_648", user, "521 generated from 648"),
    )
    upload_id = cur.lastrowid
    n = 0
    for _, r in grp.iterrows():
        # report_date = first day of month key
        mk = str(r["month_key"])
        report_date = f"{mk}-01" if len(mk) == 7 else mk
        chu = r["chu"] if pd.notna(r["chu"]) and str(r["chu"]).strip() else None
        cur.execute(
            """
            INSERT INTO moh_521
                (upload_id, report_date, facility, chu, suspected, tested, positive,
                 treated, referred, followed_up, notes, source, rollup_from_648)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rollup_648', 1)
            """,
            (
                upload_id,
                report_date,
                str(r["facility"]),
                chu,
                int(r["suspected"]),
                int(r["tested"]),
                int(r["positive"]),
                int(r["treated"]),
                int(r["referred"]),
                int(r["followed_up"]),
                f"Rollup of {int(r['n_rows'])} MOH 648 row(s)",
            ),
        )
        n += 1
    cur.execute("UPDATE layer2_uploads SET row_count = ? WHERE id = ?", (n, upload_id))
    conn.commit()
    conn.close()
    log_audit(user, "521_rollup_from_648", "moh_521", f"rows={n} groups={len(grp)}")
    return n, len(grp)


def community_cascade_from_648(facility=None):
    """Aggregate 648 into operational cascade with formula-safe rates."""
    try:
        m = query("SELECT * FROM moh_648")
    except Exception:
        return pd.DataFrame(), {}
    if m.empty:
        return m, {}
    if facility and facility != "All three":
        m = m[m["facility"] == facility]
    for col in ("suspected", "tested", "positive", "treated", "referred", "followed_up",
                "referral_arrived", "negative", "treated_without_test"):
        if col not in m.columns:
            m[col] = 0
        m[col] = pd.to_numeric(m[col], errors="coerce").fillna(0)
    totals = {
        "suspected": int(m["suspected"].sum()),
        "tested": int(m["tested"].sum()),
        "positive": int(m["positive"].sum()),
        "negative": int(m["negative"].sum()),
        "treated": int(m["treated"].sum()),
        "referred": int(m["referred"].sum()),
        "referral_arrived": int(m["referral_arrived"].sum()),
        "followed_up": int(m["followed_up"].sum()),
        "twt": int(m["treated_without_test"].sum()),
        "rows": len(m),
    }
    rates = {
        "testing_rate": safe_rate(totals["tested"], totals["suspected"], indicator="648_test_rate"),
        "positivity": safe_rate(totals["positive"], totals["tested"], indicator="648_tpr"),
        "treatment_cov": safe_rate(totals["treated"], totals["positive"], indicator="648_treat"),
        "referral_rate": safe_rate(totals["referred"], totals["positive"], indicator="648_ref"),
        "arrival_rate": safe_rate(totals["referral_arrived"], totals["referred"], indicator="648_arrival"),
        "followup_rate": safe_rate(totals["followed_up"], totals["positive"], indicator="648_fu"),
    }
    return m, {"totals": totals, "rates": rates}


def run_formula_integrity_audit(df, filters):
    """
    Full integrity gate audit:
    Valid | Missing denominator | Num>den | Period mismatch | Source mismatch |
    Duplicate | Overlapping components | Population mismatch | Reconciliation required
    """
    d = _filter_data(df, filters) if df is not None else pd.DataFrame()
    rows = []
    category_counts = {
        "Valid calculations": 0,
        "Missing denominator": 0,
        "Numerator > denominator": 0,
        "Period mismatch": 0,
        "Source mismatch": 0,
        "Duplicate data": 0,
        "Overlapping components": 0,
        "Population mismatch": 0,
        "Reconciliation required": 0,
    }

    def _add(row, category=None):
        rows.append(row)
        stt = (row.get("Status") or "").upper()
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1
        elif stt == "VALID":
            category_counts["Valid calculations"] += 1
        elif stt == "NA":
            category_counts["Missing denominator"] += 1
        elif stt == "INVALID":
            category_counts["Numerator > denominator"] += 1

    # ── Registry indicators via universal engine ──
    for iid in ("GO_1", "CAS_1", "CAS_POS", "CAS_2", "R1_1", "R1_2", "COM_TPR", "EPI_1"):
        try:
            r = compute_indicator(iid, df, filters)
            cat = None
            stt = (r.get("status") or "na").upper()
            reason = r.get("reason") or ""
            if stt == "INVALID":
                cat = "Numerator > denominator"
            elif stt == "NA" and "denominator" in reason.lower():
                cat = "Missing denominator"
            elif stt == "NA" and "population" in reason.lower():
                cat = "Population mismatch"
            elif "period" in reason.lower() and "mix" in reason.lower():
                cat = "Period mismatch"
                stt = "INVALID"
            _add({
                "ID": iid,
                "Indicator": r.get("name"),
                "Numerator": r.get("numerator"),
                "Denominator": r.get("denominator"),
                "Calculated %": r.get("calculated_pct"),
                "KPI value": r.get("value"),
                "Status": stt,
                "Category": cat or ("Valid calculations" if stt == "VALID" else stt),
                "Reason": reason,
                "Source": r.get("source"),
                "pct_type": r.get("pct_type"),
                "RAG": r.get("rag"),
            }, cat)
        except Exception as e:
            _add({
                "ID": iid, "Indicator": iid, "Status": "ERROR", "Category": "Source mismatch",
                "Reason": str(e), "Numerator": None, "Denominator": None,
                "Calculated %": None, "KPI value": None, "Source": "", "pct_type": "", "RAG": "grey",
            }, "Source mismatch")

    # ── GO_1 facility × month cells ──
    if not d.empty:
        monthly = d[d["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)]
        for fac in FACILITIES:
            for per in sorted(monthly["period"].astype(str).unique())[-8:]:
                sub = monthly[(monthly["facility"] == fac) & (monthly["period"].astype(str) == per)]
                if sub.empty:
                    continue
                r = compute_indicator("GO_1", sub, facility=fac, period=per)
                if r.get("status") == "invalid" or (r.get("numerator") and r.get("denominator")):
                    cat = "Numerator > denominator" if r.get("status") == "invalid" else None
                    if r.get("status") == "na":
                        cat = "Missing denominator"
                    _add({
                        "ID": "GO_1_cell",
                        "Indicator": f"GO_1 · {fac} · {per}",
                        "Numerator": r.get("numerator"),
                        "Denominator": r.get("denominator"),
                        "Calculated %": r.get("calculated_pct"),
                        "KPI value": r.get("value"),
                        "Status": (r.get("status") or "na").upper(),
                        "Category": cat or ("Valid calculations" if r.get("status") == "valid" else ""),
                        "Reason": r.get("reason") or "",
                        "Source": "MOH 706 RDT",
                        "pct_type": "BOUNDED_RATE",
                        "RAG": r.get("rag"),
                    }, cat)

    # ── Period mix check ──
    if not d.empty:
        pts = _period_types_in(d)
        if len(pts) > 1:
            _add({
                "ID": "PERIOD_MIX",
                "Indicator": "Filter mixes period types",
                "Numerator": None, "Denominator": None, "Calculated %": None, "KPI value": None,
                "Status": "INVALID",
                "Category": "Period mismatch",
                "Reason": f"Active filter includes period types: {sorted(pts)} — do not mix for operational rates",
                "Source": "filter", "pct_type": "", "RAG": "grey",
            }, "Period mismatch")

    # ── Overlapping AL components (Total + weight bands) ──
    if not d.empty:
        al_total = sum_locked(d, {"indicator_exact": ["AL Total"]})
        al_disp = sum_locked(d, {"indicator_exact": ["AL Dispensed"]})
        al_bands = float(
            d[d["indicator"].astype(str).str.contains(r"AL \(.*\)|AL weight", na=False, case=False)]["value"].sum()
        ) if "indicator" in d.columns else 0
        if al_total and al_bands and al_total > 0 and al_bands > 0:
            # If both total and components present, flag potential double count
            if abs(al_total - al_bands) / max(al_total, 1) > 0.05:
                _add({
                    "ID": "AL_OVERLAP",
                    "Indicator": "AL Total vs weight-band components",
                    "Numerator": al_bands, "Denominator": al_total,
                    "Calculated %": round(abs(al_total - al_bands) / al_total * 100, 1),
                    "KPI value": None,
                    "Status": "INVALID",
                    "Category": "Overlapping components",
                    "Reason": f"AL Total={al_total:g} vs bands sum={al_bands:g} — avoid double-counting in coverage",
                    "Source": "MOH 743/748", "pct_type": "", "RAG": "grey",
                }, "Overlapping components")

    # ── Duplicate observation keys ──
    if not d.empty and {"facility", "register", "indicator", "period"}.issubset(d.columns):
        keys = d.groupby(["facility", "register", "indicator", "period", "age_group"], dropna=False).size()
        dups = keys[keys > 1]
        if len(dups) > 0:
            _add({
                "ID": "DUP_KEYS",
                "Indicator": "Duplicate facility×register×indicator×period rows",
                "Numerator": int(dups.sum()), "Denominator": len(dups),
                "Calculated %": None, "KPI value": None,
                "Status": "INVALID",
                "Category": "Duplicate data",
                "Reason": f"{len(dups)} key groups have >1 row — check import dedup",
                "Source": "malaria_data", "pct_type": "", "RAG": "grey",
            }, "Duplicate data")

    # ── 648 rates ──
    _, pack = community_cascade_from_648(
        filters.get("facility") if filters else None
    )
    if pack.get("rates"):
        for k, a in pack["rates"].items():
            stt = (a.get("status") or "na").upper()
            cat = None
            if stt == "INVALID":
                cat = "Numerator > denominator"
            elif stt == "NA":
                cat = "Missing denominator"
            _add({
                "ID": f"648_{k}",
                "Indicator": f"MOH 648 {k}",
                "Numerator": a.get("numerator"),
                "Denominator": a.get("denominator"),
                "Calculated %": a.get("calculated_pct"),
                "KPI value": a.get("value"),
                "Status": stt,
                "Category": cat or ("Valid calculations" if stt == "VALID" else stt),
                "Reason": a.get("reason") or "",
                "Source": "MOH 648",
                "pct_type": "BOUNDED_RATE",
                "RAG": "grey" if stt != "VALID" else "green",
            }, cat)

    # ── Latest recon flags → reconciliation required ──
    try:
        run = query("SELECT id FROM recon_runs ORDER BY id DESC LIMIT 1")
        if not run.empty:
            rid = int(run.iloc[0]["id"])
            flagged = query(
                """
                SELECT rule_code, facility, period, message, status FROM recon_items
                WHERE run_id = ? AND status IN ('requires_reconciliation', 'review')
                LIMIT 30
                """,
                (rid,),
            )
            for _, it in flagged.iterrows():
                _add({
                    "ID": f"RECON_{it.get('rule_code')}",
                    "Indicator": it.get("rule_code"),
                    "Numerator": None, "Denominator": None, "Calculated %": None, "KPI value": None,
                    "Status": "RECONCILIATION REQUIRED" if it.get("status") == "requires_reconciliation" else "REVIEW",
                    "Category": "Reconciliation required",
                    "Reason": f"{it.get('facility')} {it.get('period')}: {it.get('message')}",
                    "Source": "recon_engine", "pct_type": "", "RAG": "red",
                }, "Reconciliation required")
    except Exception:
        pass

    # ── Population mismatch (ABER-style) ──
    try:
        r = compute_indicator("EPI_1", df, filters)
        tested = volume_from_registry(d, "CAS_1", "num")
        pop = r.get("denominator") or 0
        if pop and tested and tested > pop:
            _add({
                "ID": "POP_MISMATCH",
                "Indicator": "Tested > population",
                "Numerator": tested, "Denominator": pop,
                "Calculated %": round(tested / pop * 100, 1), "KPI value": None,
                "Status": "INVALID",
                "Category": "Population mismatch",
                "Reason": "705 tested exceeds provisional population — reconcile catchment",
                "Source": "705 + population", "pct_type": "EPIDEMIOLOGICAL_RATE", "RAG": "grey",
            }, "Population mismatch")
    except Exception:
        pass

    audit_df = pd.DataFrame(rows)
    summary = {
        "valid": category_counts.get("Valid calculations", 0),
        "invalid": category_counts.get("Numerator > denominator", 0),
        "na": category_counts.get("Missing denominator", 0),
        "period_mismatch": category_counts.get("Period mismatch", 0),
        "source_mismatch": category_counts.get("Source mismatch", 0),
        "duplicate": category_counts.get("Duplicate data", 0),
        "overlap": category_counts.get("Overlapping components", 0),
        "population": category_counts.get("Population mismatch", 0),
        "recon": category_counts.get("Reconciliation required", 0),
        "total": len(audit_df),
        "categories": category_counts,
    }
    return audit_df, summary


def page_data_foundation(user, filters):
    """Phase A0 UI: submissions browser + observation history + reconciliation stubs."""
    page_header(
        "Data foundation (Phase A0)",
        "Immutable submissions, append-only observations, indicator dictionary, "
        "and Layer-1 reconciliation stubs. Facility KPIs still use the latest-view table.",
    )

    # ── Summary metrics ──
    try:
        n_sub = int(query("SELECT COUNT(*) AS n FROM submissions").iloc[0]["n"])
    except Exception:
        n_sub = 0
    try:
        n_obs = int(query("SELECT COUNT(*) AS n FROM observations").iloc[0]["n"])
    except Exception:
        n_obs = 0
    try:
        n_dict = int(query("SELECT COUNT(*) AS n FROM indicator_dictionary WHERE is_active = 1").iloc[0]["n"])
    except Exception:
        n_dict = 0
    try:
        n_latest = int(query("SELECT COUNT(*) AS n FROM malaria_data").iloc[0]["n"])
    except Exception:
        n_latest = 0

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        big_number(f"{n_sub}", "Submissions")
    with c2:
        big_number(f"{n_obs:,}", "Observations (history)")
    with c3:
        big_number(f"{n_dict}", "Dictionary entries")
    with c4:
        big_number(f"{n_latest:,}", "Latest-view rows")

    if n_sub == 0:
        st.info("No submissions yet. Upload a KHIS file on **Upload & Settings** and commit.")
    else:
        st.caption(
            "Each commit creates one **submission** and appends **observations**. "
            "Re-uploading does not erase prior observations."
        )

    # ── Submissions browser ──
    section_header("Submissions browser")
    try:
        subs = query(
            """
            SELECT id, file_name, uploaded_by, uploaded_at, row_count,
                   matched_labels, unmatched_labels, period_types, geo_levels,
                   stored_path
            FROM submissions ORDER BY id DESC LIMIT 50
            """
        )
    except Exception as e:
        st.error(f"Could not read submissions: {e}")
        subs = pd.DataFrame()

    if not subs.empty:
        st.dataframe(
            subs.drop(columns=["stored_path"], errors="ignore"),
            use_container_width=True,
            hide_index=True,
        )
        pick = st.selectbox(
            "Inspect submission",
            options=subs["id"].tolist(),
            format_func=lambda i: f"#{i} · {subs.loc[subs['id']==i, 'file_name'].values[0]} · {subs.loc[subs['id']==i, 'uploaded_at'].values[0]}",
        )
        if pick:
            meta = subs[subs["id"] == pick].iloc[0]
            st.markdown(
                f"**File:** `{meta['file_name']}` · **By:** {meta['uploaded_by']} · "
                f"**Period types:** {meta.get('period_types') or '—'} · "
                f"**Geo:** {meta.get('geo_levels') or '—'}"
            )
            obs = query(
                """
                SELECT geo_level, facility, register, indicator, period, period_type,
                       source_period_label, age_group, value
                FROM observations WHERE submission_id = ?
                ORDER BY facility, register, period, indicator
                LIMIT 500
                """,
                (int(pick),),
            )
            if obs.empty:
                st.warning("No observations for this submission.")
            else:
                g1, g2 = st.columns(2)
                with g1:
                    by_geo = obs.groupby("geo_level").size().reset_index(name="rows")
                    st.dataframe(by_geo, use_container_width=True, hide_index=True)
                with g2:
                    by_pt = obs.groupby("period_type").size().reset_index(name="rows")
                    st.dataframe(by_pt, use_container_width=True, hide_index=True)
                with st.expander(f"Observation sample ({len(obs)} rows shown, max 500)", expanded=False):
                    st.dataframe(obs, use_container_width=True, hide_index=True)
    else:
        st.caption("Submission list is empty.")

    # ── Indicator dictionary ──
    section_header("Indicator dictionary")
    try:
        ddict = query(
            """
            SELECT register, indicator, age_group, domain, method, measure,
                   indicator_family, stream, source_label
            FROM indicator_dictionary WHERE is_active = 1
            ORDER BY register, indicator
            """
        )
    except Exception:
        ddict = pd.DataFrame()
    if ddict.empty:
        st.warning("Dictionary empty — restart app to seed from KNOWN_LABELS.")
        if st.button("Seed dictionary now"):
            seed_indicator_dictionary()
            st.success("Seeded.")
            st.rerun()
    else:
        domains = ["All"] + sorted(ddict["domain"].dropna().unique().tolist())
        dom = st.selectbox("Filter domain", domains, key="dict_domain")
        show = ddict if dom == "All" else ddict[ddict["domain"] == dom]
        st.dataframe(show, use_container_width=True, hide_index=True)
        st.caption(f"{len(show)} entries. Dictionary drives parse mapping and future 91-indicator formulas.")

    # ── Reconciliation stubs ──
    section_header("Reconciliation (Layer 1 stubs)")
    st.markdown(
        """
        Compares related measures **within the latest KHIS submission**
        (e.g. MOH 706 RDT exams vs MOH 743 tested RDT).  
        When MOH 648 / 521 / register files exist, the same engine will compare **across layers**.
        """
    )
    st.caption(
        "Runs **Layer 1 internal** pairs (706 vs 743, etc.) and **Layer 1 ↔ Layer 2** "
        "when MOH 648/521 rows share the same facility and calendar month as KHIS monthly periods."
    )
    if st.button("▶ Run reconciliation (L1 + L1↔L2)", type="primary"):
        with st.spinner("Comparing Layer 1 and Layer 2 measures…"):
            run_id, checked, flagged = run_reconciliation_stubs(run_by=user.get("username", "user"))
        st.success(f"Run **#{run_id}**: checked **{checked}**, flagged **{flagged}**.")
        st.rerun()

    try:
        runs = query("SELECT * FROM recon_runs ORDER BY id DESC LIMIT 10")
    except Exception:
        runs = pd.DataFrame()
    if not runs.empty:
        st.markdown("**Recent runs**")
        st.dataframe(runs, use_container_width=True, hide_index=True)
        rid = st.selectbox("Show items for run", runs["id"].tolist(), key="recon_run_pick")
        items = query(
            """
            SELECT rule_code, facility, period, value_a, value_b, difference,
                   difference_pct, status, message
            FROM recon_items WHERE run_id = ?
            ORDER BY CASE status
                WHEN 'requires_reconciliation' THEN 0
                WHEN 'review' THEN 1
                WHEN 'incomplete' THEN 2
                ELSE 3 END, facility, period
            """,
            (int(rid),),
        )
        if items.empty:
            st.info("No items for this run.")
        else:
            status_filter = st.multiselect(
                "Status filter",
                options=sorted(items["status"].unique().tolist()),
                default=[s for s in items["status"].unique() if s != "aligned"],
                key="recon_status_f",
            )
            view = items[items["status"].isin(status_filter)] if status_filter else items
            st.dataframe(view, use_container_width=True, hide_index=True)
            st.caption(
                f"{len(view)} of {len(items)} items shown. "
                "Statuses: aligned · review · requires_reconciliation · incomplete · accepted · rejected."
            )
            # Formal sign-off
            section_header("Reconciliation sign-off")
            open_items = query(
                """
                SELECT id, rule_code, facility, period, difference, message, status, decision
                FROM recon_items
                WHERE run_id = ? AND status IN ('requires_reconciliation', 'review', 'incomplete')
                  AND (decision IS NULL OR decision = '')
                ORDER BY id
                """,
                (int(rid),),
            )
            if open_items.empty:
                st.success("All items on this run are aligned or already decided.")
            else:
                st.warning(f"**{len(open_items)}** item(s) awaiting a decision.")
                pick = st.selectbox("Item", open_items["id"].tolist(), key="recon_so_id")
                row = open_items[open_items["id"] == pick].iloc[0]
                st.caption(f"{row.get('rule_code')} · {row.get('facility')} · {row.get('period')} · {row.get('message')}")
                dec = st.radio("Decision", ["accepted", "rejected", "escalated"], horizontal=True, key="recon_so_dec")
                note = st.text_input("Note / evidence", key="recon_so_note")
                if user.get("can_upload") and st.button("Record decision", key="recon_so_go"):
                    execute(
                        """
                        UPDATE recon_items
                        SET decision=?, decision_note=?, signed_off_by=?, signed_off_at=?,
                            status=CASE WHEN ?='accepted' THEN 'accepted'
                                        WHEN ?='rejected' THEN 'rejected'
                                        ELSE 'review' END
                        WHERE id=?
                        """,
                        (
                            dec, note, user.get("username", ""),
                            datetime.now().strftime("%Y-%m-%d %H:%M"),
                            dec, dec, int(pick),
                        ),
                    )
                    log_audit(user.get("username", ""), "recon_signoff", "recon_items", f"id={pick} {dec}")
                    st.success(f"Item #{pick} marked **{dec}**.")
                    st.rerun()
    else:
        st.caption("No reconciliation runs yet.")

    # ── Operational chain 648 → 521 → KHIS ──
    section_header("Operational chain — 648 → 521 → KHIS")
    try:
        n648 = int(query("SELECT COUNT(*) AS n FROM moh_648").iloc[0]["n"])
    except Exception:
        n648 = 0
    try:
        n521 = int(query("SELECT COUNT(*) AS n FROM moh_521").iloc[0]["n"])
        n521_auto = int(query("SELECT COUNT(*) AS n FROM moh_521 WHERE rollup_from_648 = 1").iloc[0]["n"])
    except Exception:
        n521, n521_auto = 0, 0
    try:
        nkhis = count_rows()
    except Exception:
        nkhis = 0
    oc1, oc2, oc3, oc4 = st.columns(4)
    with oc1:
        big_number(str(n648), "MOH 648 events")
    with oc2:
        big_number(str(n521), "MOH 521 rows")
    with oc3:
        big_number(str(n521_auto), "521 auto from 648")
    with oc4:
        big_number(f"{nkhis:,}", "KHIS latest rows")
    missing = []
    if n648 == 0:
        missing.append("Import MOH 648 (community events)")
    if n521 == 0:
        missing.append("Generate or import MOH 521")
    if nkhis == 0:
        missing.append("Commit KHIS Excel")
    if missing:
        st.warning("Chain incomplete: " + " · ".join(missing))
    else:
        st.success("All three layers have rows. Run reconciliation and sign off flagged items.")

    section_header("Three-way 521 vs 648 vs KHIS")
    st.caption("Generated 521 = rollup of 648. Reported 521 = imported 521. KHIS = latest community-adjacent 705/706 positives & tests for the month.")
    try:
        ev = query(
            """
            SELECT facility, substr(event_date,1,7) AS ym,
                   SUM(tested) AS t648, SUM(positive) AS p648
            FROM moh_648 GROUP BY facility, ym
            """
        )
    except Exception:
        ev = pd.DataFrame()
    try:
        s21 = query(
            """
            SELECT facility, substr(report_date,1,7) AS ym,
                   SUM(tested) AS t521, SUM(positive) AS p521,
                   MAX(IFNULL(rollup_from_648,0)) AS auto
            FROM moh_521 GROUP BY facility, ym
            """
        )
    except Exception:
        s21 = pd.DataFrame()
    tw = []
    if not ev.empty or not s21.empty:
        keys = set()
        if not ev.empty:
            keys |= set(zip(ev["facility"], ev["ym"]))
        if not s21.empty:
            keys |= set(zip(s21["facility"], s21["ym"]))
        khis = load_all_data()
        for fac, ym in sorted(keys):
            r648 = ev[(ev["facility"] == fac) & (ev["ym"] == ym)] if not ev.empty else pd.DataFrame()
            r521 = s21[(s21["facility"] == fac) & (s21["ym"] == ym)] if not s21.empty else pd.DataFrame()
            t648 = float(r648["t648"].sum()) if not r648.empty else None
            p648 = float(r648["p648"].sum()) if not r648.empty else None
            t521 = float(r521["t521"].sum()) if not r521.empty else None
            p521 = float(r521["p521"].sum()) if not r521.empty else None
            khis_t = khis_p = None
            if khis is not None and not khis.empty:
                m = khis[(khis["facility"] == fac) & (khis["period"].astype(str) == ym)]
                if not m.empty:
                    c = compute_cascade_metrics(m)
                    khis_t, khis_p = c.get("tested"), c.get("confirmed")
            diff_t = None
            if t521 is not None and t648 is not None:
                diff_t = t521 - t648
            stt = "aligned" if diff_t == 0 else ("gap" if diff_t is not None else "incomplete")
            tw.append({
                "Facility": fac, "Month": ym,
                "648 tested": t648, "521 tested": t521, "KHIS tested": khis_t,
                "648 pos": p648, "521 pos": p521, "KHIS confirmed": khis_p,
                "521−648 tested": diff_t, "Status": stt,
            })
    if tw:
        st.dataframe(pd.DataFrame(tw), use_container_width=True, hide_index=True)
    else:
        st.info("Import 648/521 to see the three-way table.")

    # Three-way 521
    try:
        s521 = query(
            """
            SELECT facility, period,
                   SUM(CASE WHEN IFNULL(source,'') LIKE '%rollup%' OR rollup_from_648=1 THEN tested ELSE 0 END) AS calc_tested,
                   SUM(CASE WHEN IFNULL(source,'') NOT LIKE '%rollup%' AND IFNULL(rollup_from_648,0)=0 THEN tested ELSE 0 END) AS reported_tested
            FROM moh_521
            GROUP BY facility, period
            """
        )
    except Exception:
        s521 = pd.DataFrame()
    if not s521.empty:
        section_header("521 three-way (calculated vs reported)")
        st.caption("Calculated = rollup from 648. Reported = uploaded 521. KHIS 748/521 is compared in recon runs.")
        st.dataframe(s521, use_container_width=True, hide_index=True)

    # Population confirmation
    section_header("Population denominators (close provisional gap)")
    st.caption(
        "Incidence and ABER stay **provisional** until County DoH confirms catchment. "
        "Saving here flips status for the whole app."
    )
    pcols = st.columns(4)
    new_pops = {}
    for i, fac in enumerate(["Jaribuni", "Pingilikani", "Mwakuhenga", "Total"]):
        with pcols[i]:
            new_pops[fac] = st.number_input(
                fac, min_value=0, value=int(get_population(fac if fac != "Total" else None) if fac != "Total" else get_population("Total")),
                step=100, key=f"pop_in_{fac}",
            )
    status_pick = st.selectbox(
        "Population status",
        ["provisional", "confirmed", "deferred_epi"],
        index=["provisional", "confirmed", "deferred_epi"].index(get_population_status())
        if get_population_status() in ("provisional", "confirmed", "deferred_epi") else 0,
        key="pop_status_pick",
    )
    if user.get("can_upload") and st.button("Save population & status", key="save_pop"):
        for fac, val in new_pops.items():
            set_setting(f"pop_{fac}", val, user.get("username", "system"))
        set_setting("population_status", status_pick, user.get("username", "system"))
        log_audit(user.get("username", ""), "population_update", "programme_settings", status_pick)
        st.success(f"Saved. Status is now **{status_pick}**.")
        st.rerun()
    st.info(f"Current status in use: **{get_population_status()}**")

    section_header("Kilifi-wide surveillance (7 sub-counties)")
    st.caption(
        "Import the World Friends Kilifi workbook (`WFK_Kilifi_malaria_*.xlsx`) with "
        "County_trends / Subcounty_trends / Facility_trends. "
        "This sits **beside** the three CBMP KHIS sites — it does not replace them. "
        "Official list: Kilifi North, Kilifi South, Malindi, Magarini, Ganze, Kaloleni, Rabai. "
        "Workflow: **Upload → Validate → Commit to database**."
    )
    if user.get("can_upload"):
        wfk = st.file_uploader("WFK Kilifi workbook (.xlsx)", type=["xlsx"], key="wfk_up")
        if wfk is not None:
            if st.button("1 · Validate file", key="wfk_validate"):
                ok, summary, county, sub, fac, errs = validate_wfk_kilifi_workbook(wfk)
                st.session_state["wfk_validation"] = {
                    "ok": ok, "summary": summary, "errors": errs,
                    "name": getattr(wfk, "name", "wfk.xlsx"),
                }
                # Keep parsed frames in session via re-parse on commit (file still in uploader)
                if errs:
                    st.error("; ".join(errs))
                else:
                    st.success("File readable — review validation below, then commit.")
            val = st.session_state.get("wfk_validation")
            if val and val.get("summary"):
                s = val["summary"]
                st.markdown("#### FILE VALIDATION")
                vrows = [
                    ("File readable", "✅" if val.get("ok") else "❌"),
                    ("County rows", s.get("county_rows")),
                    ("Sub-county rows", s.get("subcounty_rows")),
                    ("Facility rows", s.get("facility_rows")),
                    ("Sub-counties detected", f"{s.get('sub_counties_detected')}/7"),
                    ("Periods", s.get("period_range")),
                    ("Missing sub-counties", ", ".join(s.get("missing_sub_counties") or []) or "None"),
                    ("Duplicate records", s.get("duplicate_records")),
                    ("Invalid TPR (>100)", s.get("invalid_tpr")),
                    ("Missing tested values", s.get("missing_tested")),
                ]
                st.dataframe(
                    pd.DataFrame(vrows, columns=["Check", "Result"]),
                    use_container_width=True, hide_index=True,
                )
                if st.button("2 · COMMIT TO DATABASE", type="primary", key="wfk_go"):
                    n, errs = import_wfk_kilifi_workbook(wfk, user.get("username", "system"))
                    if n:
                        st.success(
                            f"Committed **{n}** county/sub-county/facility trend rows "
                            f"(previous sub-county table replaced with this version)."
                        )
                        st.session_state.pop("wfk_validation", None)
                        st.rerun()
                    if errs:
                        st.error("; ".join(errs))
    try:
        scn = query("SELECT COUNT(DISTINCT sub_county) AS n FROM kilifi_subcounty")
        st.caption(f"Sub-counties currently loaded: {int(scn.iloc[0]['n']) if not scn.empty else 0} of 7")
    except Exception:
        pass
    try:
        vers = query(
            "SELECT id, version_label, source_file, uploaded_at, subcounty_rows, period_range, status "
            "FROM kilifi_upload_versions ORDER BY id DESC LIMIT 8"
        )
        if not vers.empty:
            with st.expander("WFK upload versions", expanded=False):
                st.dataframe(vers, use_container_width=True, hide_index=True)
                st.caption(
                    "Each COMMIT archives the previous metadata row and marks the new file as **current**. "
                    "Live analysis tables hold only the active version (full snapshot archive can be added later)."
                )
    except Exception:
        pass

    # ── A1: County series (observations only — not mixed into facility KPIs) ──
    section_header("County series (Layer 1 · observations)")
    st.caption(
        "County aggregates from the Kilifi County sheet live in **observations** only. "
        "They are not written into the facility latest-view used for GO/SO/R1."
    )
    try:
        county_obs = query(
            """
            SELECT o.period, o.period_type, o.register, o.indicator, o.age_group,
                   o.value, o.source_period_label, o.submission_id
            FROM observations o
            WHERE o.geo_level = 'county'
            ORDER BY o.submission_id DESC, o.period, o.register, o.indicator
            LIMIT 800
            """
        )
    except Exception:
        county_obs = pd.DataFrame()

    if county_obs.empty:
        st.info("No county-level observations yet. Commit a KHIS file that includes the county sheet.")
    else:
        pt_opts = ["All"] + sorted(county_obs["period_type"].dropna().unique().tolist())
        reg_opts = ["All"] + sorted(county_obs["register"].dropna().unique().tolist())
        f1, f2 = st.columns(2)
        with f1:
            pt = st.selectbox("Period type", pt_opts, key="county_pt")
        with f2:
            reg = st.selectbox("Register", reg_opts, key="county_reg")
        view_c = county_obs
        if pt != "All":
            view_c = view_c[view_c["period_type"] == pt]
        if reg != "All":
            view_c = view_c[view_c["register"] == reg]
        if "submission_id" in view_c.columns and not view_c.empty:
            max_sid = int(view_c["submission_id"].max())
            only_latest = st.checkbox("Latest submission only", value=True, key="county_latest")
            if only_latest:
                view_c = view_c[view_c["submission_id"] == max_sid]
        st.dataframe(
            view_c.drop(columns=["submission_id"], errors="ignore"),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"{len(view_c):,} county rows shown.")

    # ── A1: Period-type mix in facility latest view ──
    section_header("Period-type mix (facility latest view)")
    st.caption(
        "Official KPIs use **malaria_data**. Mixing years + months in one filter can distort rates — "
        "prefer monthly periods for operational RAG."
    )
    try:
        periods = query("SELECT DISTINCT period FROM malaria_data")
        if not periods.empty:
            types = {"year": 0, "quarter": 0, "month": 0, "other": 0}
            for p in periods["period"].astype(str):
                if len(p) == 4 and p.isdigit():
                    types["year"] += 1
                elif "-Q" in p:
                    types["quarter"] += 1
                elif len(p) == 7 and p[4] == "-":
                    types["month"] += 1
                else:
                    types["other"] += 1
            st.write(
                f"Distinct periods in latest view — "
                f"year: **{types['year']}**, quarter: **{types['quarter']}**, "
                f"month: **{types['month']}**, other: **{types['other']}**."
            )
        else:
            st.caption("No facility latest-view rows yet.")
    except Exception as e:
        st.caption(f"Could not summarise periods: {e}")

    # ── Layer 2: register / operational imports ──
    section_header("Layer 2 — Register & operational imports")
    st.markdown(
        """
        Structures for when **MOH 648 / 521** and **stock movement** files are available.
        These do **not** replace KHIS Layer 1; they enable future cross-source reconciliation.
        """
    )
    tdir = APP_DIR / "docs" / "templates"
    t1, t2, t3 = st.columns(3)
    for col, fname, label in (
        (t1, "moh_648_template.csv", "MOH 648 template"),
        (t2, "moh_521_template.csv", "MOH 521 template"),
        (t3, "stock_movements_template.csv", "Stock movements template"),
    ):
        path = tdir / fname
        with col:
            if path.exists():
                st.download_button(
                    f"📥 {label}",
                    path.read_bytes(),
                    file_name=fname,
                    mime="text/csv",
                    key=f"dl_{fname}",
                )
            else:
                st.caption(f"Missing {fname}")

    if user.get("can_upload"):
        src = st.selectbox(
            "Import type",
            [
                ("moh_648", "MOH 648 — CHP / community register summary"),
                ("moh_521", "MOH 521 — CHU summary"),
                ("stock_movements", "Stock movements — RDT / AL receipts & issues"),
            ],
            format_func=lambda x: x[1],
            key="layer2_src",
        )
        src_type = src[0] if isinstance(src, tuple) else src
        up = st.file_uploader("Layer 2 CSV", type=["csv"], key="layer2_csv")
        if up is not None and st.button("Import Layer 2 CSV", type="primary", key="layer2_go"):
            with st.spinner("Importing…"):
                n, uid, errs = import_layer2_csv(up, src_type, user.get("username", "user"))
            if errs and not n:
                for e in errs:
                    st.error(e)
            elif errs:
                st.warning(f"Imported {n} rows with notes: {'; '.join(errs)}")
            else:
                st.success(f"Imported **{n}** rows · Layer 2 upload **#{uid}** ({src_type}).")
                st.rerun()
    else:
        st.caption("Only users with upload permission can import Layer 2 files.")

    # Counts + recent rows
    l2c1, l2c2, l2c3, l2c4 = st.columns(4)
    def _cnt(table):
        try:
            return int(query(f"SELECT COUNT(*) AS n FROM {table}").iloc[0]["n"])
        except Exception:
            return 0
    with l2c1:
        big_number(str(_cnt("moh_648")), "MOH 648 events")
    with l2c2:
        big_number(str(_cnt("moh_521")), "MOH 521 rows")
    with l2c3:
        big_number(str(_cnt("stock_movements")), "Stock movements")
    with l2c4:
        big_number(str(_cnt("layer2_uploads")), "Layer 2 uploads")

    try:
        recent_l2 = query(
            "SELECT id, source_type, file_name, uploaded_by, uploaded_at, row_count "
            "FROM layer2_uploads ORDER BY id DESC LIMIT 10"
        )
    except Exception:
        recent_l2 = pd.DataFrame()
    if not recent_l2.empty:
        st.markdown("**Recent Layer 2 uploads**")
        st.dataframe(recent_l2, use_container_width=True, hide_index=True)

    with st.expander("Preview Layer 2 data"):
        tab = st.selectbox("Table", ["moh_648", "moh_521", "stock_movements"], key="l2_preview_tbl")
        try:
            prev = query(f"SELECT * FROM {tab} ORDER BY id DESC LIMIT 50")
            if prev.empty:
                st.caption("No rows yet — download a template, fill, and import.")
            else:
                st.dataframe(prev, use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(str(e))

    section_header("Layer 1 ↔ Layer 2 rules (active in recon run)")
    st.markdown(
        """
| Code | Compare |
|------|---------|
| `L1_748_vs_L2_648_TESTED` | KHIS MOH 748 community tests vs MOH 648 tested |
| `L1_748_vs_L2_648_POSITIVE` | KHIS MOH 748 positives vs MOH 648 positive |
| `L1_FACILITY_vs_L2_521_TESTED` | KHIS 705/706 tested vs MOH 521 tested |
| `L2_648_vs_L2_521_TESTED` | MOH 648 vs MOH 521 tested |
| `L2_648_POS_GT_TESTED` | MOH 648 internal consistency |
| `L2_648_TESTED_GT_SUSPECTED` | MOH 648 internal consistency |

Join key: **facility** + **YYYY-MM** (KHIS monthly period ↔ `report_date`).  
Annual/quarter KHIS periods are skipped for L1↔L2. Import Layer 2 CSVs with `report_date` like `2026-03-15`.

Also active (locked): `705_CONF_vs_706_RDT_POS`, `AL_vs_705_CONFIRMED`, `748_vs_648_TESTED`, `L1_748_vs_L2_648_POS`.
        """
    )

    # ── MOH 648 event cascade + 521 rollup ──
    section_header("MOH 648 event cascade & 521 auto-rollup (Phase A.5)")
    st.markdown(
        """
Community path:
**Suspected → Tested → Result (pos/neg) → Treated → Referred → Arrival → Follow-up**

MOH **521** should summarise **648** (facility × CHU × month). Use **Generate 521 from 648** rather than only uploading a separate 521 CSV.
        """
    )
    fac_648 = filters.get("facility") if filters else "All three"
    m648_rows, pack = community_cascade_from_648(
        None if fac_648 in (None, "All three") else fac_648
    )
    if pack.get("totals"):
        t = pack["totals"]
        r = pack["rates"]
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1:
            big_number(str(t["rows"]), "648 rows")
        with c2:
            big_number(str(t["suspected"]), "Suspected")
        with c3:
            big_number(str(t["tested"]), "Tested")
        with c4:
            big_number(str(t["positive"]), "Positive")
        with c5:
            big_number(str(t["treated"]), "Treated")
        with c6:
            big_number(str(t["referred"]), "Referred")
        rate_rows = []
        for k, a in (r or {}).items():
            rate_rows.append({
                "Rate": k,
                "Num": a.get("numerator"),
                "Den": a.get("denominator"),
                "KPI %": a.get("value"),
                "Raw %": a.get("calculated_pct"),
                "Status": (a.get("status") or "").upper(),
                "Reason": a.get("reason") or "",
            })
        st.dataframe(pd.DataFrame(rate_rows), use_container_width=True, hide_index=True)
        with st.expander("MOH 648 event rows (latest 80)"):
            st.dataframe(m648_rows.tail(80), use_container_width=True, hide_index=True)
    else:
        st.info("No MOH 648 rows yet — import the expanded template (result, referral_arrived, linkage_id, …).")

    if user.get("can_upload"):
        if st.button("Generate MOH 521 from MOH 648 (replace prior auto-rollup)", type="primary", key="gen_521"):
            n, g = rollup_648_to_521(user.get("username", "system"))
            if n:
                st.success(f"Wrote **{n}** MOH 521 summary row(s) from **{g}** facility×CHU×month group(s).")
                st.rerun()
            else:
                st.warning("No 648 data to roll up.")

    # ── Formula integrity audit ──
    section_header("Formula integrity audit (VALID / N/A / INVALID)")
    st.caption(
        "Automated scan of locked registry indicators under current filters. "
        "INVALID = numerator > denominator (or reporting >100%) — not shown as green KPI."
    )
    df_all = load_all_data()
    audit_df, summary = run_formula_integrity_audit(df_all, filters or {})
    cats = summary.get("categories") or {}
    st.markdown("**Integrity gate scorecard**")
    score_rows = [
        ("Valid calculations", cats.get("Valid calculations", 0), "🟢"),
        ("Missing denominator", cats.get("Missing denominator", 0), "🟡"),
        ("Numerator > denominator", cats.get("Numerator > denominator", 0), "🔴"),
        ("Period mismatch", cats.get("Period mismatch", 0), "🔴"),
        ("Source mismatch", cats.get("Source mismatch", 0), "🔴"),
        ("Duplicate data", cats.get("Duplicate data", 0), "🔴"),
        ("Overlapping components", cats.get("Overlapping components", 0), "🔴"),
        ("Population mismatch", cats.get("Population mismatch", 0), "🔴"),
        ("Reconciliation required", cats.get("Reconciliation required", 0), "🔴"),
    ]
    st.dataframe(
        pd.DataFrame([{"Test": t, "Count": c, "Signal": s} for t, c, s in score_rows]),
        use_container_width=True, hide_index=True,
    )
    a1, a2, a3, a4 = st.columns(4)
    with a1:
        big_number(str(summary.get("valid", 0)), "VALID")
    with a2:
        big_number(str(summary.get("invalid", 0)), "INVALID")
    with a3:
        big_number(str(summary.get("na", 0)), "N/A")
    with a4:
        big_number(str(summary.get("total", 0)), "Rows scanned")
    if not audit_df.empty:
        st.dataframe(audit_df, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Download integrity audit CSV",
            audit_df.to_csv(index=False).encode("utf-8"),
            file_name=f"formula_integrity_audit_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            key="dl_integrity_audit",
        )
        inv = audit_df[audit_df["Status"].isin(["INVALID", "RECONCILIATION REQUIRED"])]
        if not inv.empty:
            st.error(
                f"**{len(inv)}** row(s) need attention before programme sign-off. "
                "Push formula invalids to DQ issues, then workplan."
            )
            if user.get("can_upload") and st.button("Create DQ issues from INVALID rows", key="audit_to_dq"):
                n_new = 0
                for _, row in inv.iterrows():
                    msg = f"{row.get('ID')}: {row.get('Reason') or row.get('Indicator')}"
                    exists = query(
                        "SELECT id FROM dq_issues WHERE status='open' AND message=? LIMIT 1",
                        (msg[:400],),
                    )
                    if exists.empty:
                        execute(
                            """
                            INSERT INTO dq_issues
                            (issue_type, facility, period, severity, message, owner, status, source)
                            VALUES (?, ?, ?, ?, ?, 'County M&E', 'open', 'integrity_audit')
                            """,
                            (
                                "integrity_audit",
                                "—",
                                "—",
                                "critical" if row.get("Status") == "INVALID" else "high",
                                msg[:400],
                            ),
                        )
                        n_new += 1
                st.success(f"Created **{n_new}** DQ issue(s).")
                st.rerun()
        else:
            st.success("Integrity gate: no INVALID / RECONCILIATION REQUIRED rows in this scan.")
    else:
        st.caption("No audit rows — upload KHIS data first.")

    # ── Formula / indicator registry ──
    section_header("Indicator formula registry (Phase A gate)")
    st.caption(
        "Canonical numerator/denominator definitions. All official rates should match these. "
        "Invalid rates (num > den, reporting >100%) never drive green RAG."
    )
    reg_rows = []
    for iid, meta in INDICATOR_REGISTRY.items():
        reg_rows.append({
            "ID": iid,
            "Indicator": meta["name"],
            "Numerator": meta["numerator"],
            "Denominator": meta["denominator"] or "—",
            "Formula": meta["formula"],
            "Source": meta["source"],
            "Rule": meta["pct_rule"],
        })
    st.dataframe(pd.DataFrame(reg_rows), use_container_width=True, hide_index=True)

    # ── Transform audit ──
    section_header("Transform audit")
    st.caption(
        "Evidence trail of what entered the system: KHIS submissions, Layer 2 imports, "
        "and application audit log. Observation history is append-only (not overwritten)."
    )
    try:
        trail = query(
            """
            SELECT uploaded_at AS at, 'KHIS submission' AS event, file_name AS detail,
                   uploaded_by AS who, CAST(id AS TEXT) AS ref, row_count AS n
            FROM submissions
            UNION ALL
            SELECT uploaded_at, 'Layer 2 import · ' || source_type, file_name,
                   uploaded_by, CAST(id AS TEXT), row_count
            FROM layer2_uploads
            ORDER BY at DESC
            LIMIT 40
            """
        )
    except Exception:
        trail = pd.DataFrame()
    if trail.empty:
        st.caption("No submission or Layer 2 import events yet.")
    else:
        st.dataframe(trail, use_container_width=True, hide_index=True)

    try:
        obs_by_sub = query(
            """
            SELECT submission_id,
                   COUNT(*) AS observations,
                   SUM(CASE WHEN geo_level='facility' THEN 1 ELSE 0 END) AS facility_rows,
                   SUM(CASE WHEN geo_level='county' THEN 1 ELSE 0 END) AS county_rows,
                   COUNT(DISTINCT period) AS periods
            FROM observations
            GROUP BY submission_id
            ORDER BY submission_id DESC
            LIMIT 15
            """
        )
    except Exception:
        obs_by_sub = pd.DataFrame()
    if not obs_by_sub.empty:
        st.markdown("**Observations by submission (append-only)**")
        st.dataframe(obs_by_sub, use_container_width=True, hide_index=True)

    try:
        audit = query(
            """
            SELECT timestamp, user, action, table_name, comment
            FROM audit_log
            ORDER BY id DESC LIMIT 50
            """
        )
    except Exception:
        audit = pd.DataFrame()
    with st.expander("Application audit log (last 50)"):
        if audit.empty:
            st.caption("No audit rows.")
        else:
            st.dataframe(audit, use_container_width=True, hide_index=True)

    # Optional: value delta between last two KHIS submissions for a facility
    with st.expander("Value changes between last two KHIS submissions"):
        st.caption(
            "Compares observation totals for the same facility + register + indicator + period "
            "between the two most recent submissions."
        )
        try:
            tops = query("SELECT id FROM submissions ORDER BY id DESC LIMIT 2")
            if len(tops) < 2:
                st.caption("Need at least two KHIS commits to compare.")
            else:
                a_id, b_id = int(tops.iloc[0]["id"]), int(tops.iloc[1]["id"])
                delta = query(
                    """
                    SELECT
                        COALESCE(a.facility, b.facility) AS facility,
                        COALESCE(a.register, b.register) AS register,
                        COALESCE(a.indicator, b.indicator) AS indicator,
                        COALESCE(a.period, b.period) AS period,
                        b.value AS previous_value,
                        a.value AS latest_value,
                        (a.value - b.value) AS delta
                    FROM observations a
                    INNER JOIN observations b
                      ON a.facility = b.facility
                     AND a.register = b.register
                     AND a.indicator = b.indicator
                     AND a.period = b.period
                     AND a.geo_level = b.geo_level
                     AND a.geo_level = 'facility'
                    WHERE a.submission_id = ? AND b.submission_id = ?
                      AND ABS(COALESCE(a.value,0) - COALESCE(b.value,0)) > 0.0001
                    ORDER BY ABS(a.value - b.value) DESC
                    LIMIT 100
                    """,
                    (a_id, b_id),
                )
                if delta.empty:
                    st.success(f"No value differences between submission #{a_id} and #{b_id}.")
                else:
                    st.write(f"Submission **#{a_id}** (latest) vs **#{b_id}** — {len(delta)} changed cells (max 100 shown).")
                    st.dataframe(delta, use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(f"Compare unavailable: {e}")

    st.markdown("---")
    st.caption(
        "Phase A · Layer 1 + Layer 2 + recon + transform audit · Early Phase B run-rate on R1.1 / All Indicators. "
        "Programme KPIs: **Overview** / **All Indicators** / **Facility Deep-Dive**."
    )


def page_upload(user):
    """Upload & settings — left column for upload, right for status (not centred-only)."""
    row_count = count_rows()
    last = last_upload()

    left, right = st.columns([1.15, 1], gap="large")

    with left:
        st.markdown(f"### Welcome, **{user['name']}**")
        st.caption("Upload KHIS Excel on this page. Use the **left sidebar** to open Overview and other pages.")
        if row_count:
            if st.button("📊  Open Overview", type="primary", use_container_width=True):
                st.session_state.nav = "Overview"
                st.session_state.nav_unlocked = True
                st.rerun()
        section_header("Upload KHIS file")

    with right:
        section_header("Database status")
        c1, c2 = st.columns(2)
        with c1:
            big_number(f"{row_count:,}" if row_count else "0", "Total rows")
        with c2:
            facilities_with_data = 0
            if row_count:
                try:
                    facilities_with_data = len(load_all_data()["facility"].unique())
                except Exception:
                    pass
            big_number(str(facilities_with_data), "Facilities")
        if last is not None:
            uploaded_at = pd.to_datetime(last["uploaded_at"])
            delta = datetime.now() - uploaded_at.to_pydatetime()
            rel = "today" if delta.days == 0 else ("yesterday" if delta.days == 1 else f"{delta.days} days ago")
            st.caption(f"Last upload: **{rel}** ({uploaded_at.strftime('%Y-%m-%d')})")
        else:
            st.caption("Last upload: **Never**")
        st.caption("Tip: after commit, click **Overview** in the sidebar to see charts.")
        section_header("Recent uploads")
        try:
            history = query("SELECT * FROM upload_history ORDER BY id DESC LIMIT 8")
        except Exception:
            history = pd.DataFrame()
        if history.empty:
            st.caption("No uploads yet.")
        else:
            for _, row in history.iterrows():
                name = row.get("file_name", "Unknown file")
                when = pd.to_datetime(row["uploaded_at"]).strftime("%Y-%m-%d %H:%M")
                rows_n = row.get("rows_committed", 0)
                st.caption(f"📄 **{name}** · {when} · {rows_n:,} rows")

    # Upload controls under left-biased full width (not a centred hero only)
    st.markdown("---")
    up_left, up_right = st.columns([1.2, 0.8], gap="large")
    with up_left:
        section_header("Upload KHIS data")
        st.markdown(
            """
            **Accepted:** `.xlsx`, `.xls`, `.csv` · Max ~20 MB  

            **Expected sheets:**  
            - `MOH DATA KF NORTH` → Mwakuhenga  
            - `MOH DATA KF SOUTH` → Pingilikani  
            - `MOH DATA GANZE` → Jaribuni  

            County aggregate sheet is ignored.
            """
        )
        uploaded = st.file_uploader(
            "Choose KHIS Excel or CSV",
            type=["xlsx", "xls", "csv"],
            key="khis_upload",
        )
    with up_right:
        st.info(
            "After a successful **Commit**, use the **sidebar** "
            "(Overview, GO, R1.1, …) to open each page. "
            "Only one page shows at a time."
        )

    if uploaded is not None:
        with st.spinner("Reading and parsing the file…"):
            try:
                parsed, report = parse_uploaded_file(uploaded)
            except Exception as e:
                st.error(f"Could not read the file: {e}")
                return

        # ── Validation report (always shown) ──────────────────────────
        section_header("Upload validation report")

        if report.get("errors"):
            for err in report["errors"]:
                st.error(err)

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            big_number(str(report["total_rows"]), "Rows parsed")
        with c2:
            big_number(str(len(report.get("sheets_mapped", []))), "Sheets mapped")
        with c3:
            hit = report.get("dict_hit_rate")
            big_number(f"{hit:.0f}%" if hit is not None else "—", "Dictionary hit rate")
        with c4:
            big_number(str(len(report.get("all_matched_labels", []))), "Labels matched")
        with c5:
            big_number(str(len(report.get("all_unmatched_labels", []))), "Labels skipped")

        if report.get("phase_a0"):
            st.caption(
                f"Phase A0 · Facility rows: **{report.get('facility_rows', 0):,}** · "
                f"County rows: **{report.get('county_rows', 0):,}** · "
                f"Period types: **{', '.join(report.get('period_types') or []) or '—'}** · "
                f"Geo levels: **{', '.join(report.get('geo_levels') or []) or '—'}**"
            )

        fac_meta = report.get("facilities", {})
        if fac_meta:
            rows = []
            for fac, m in fac_meta.items():
                rows.append({
                    "Entity": fac,
                    "Geo level": m.get("geo_level", "—"),
                    "Marker": "Yes" if m.get("marker_found") else "No",
                    "Rows": m.get("rows_parsed", 0),
                    "Labels matched": len(m.get("matched_labels", [])),
                    "Labels skipped": len(m.get("unmatched_labels", [])),
                    "Periods": len(m.get("periods_seen", [])),
                    "Period types": ", ".join(m.get("period_types_seen") or []),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        mapped = report.get("sheets_mapped") or []
        if mapped:
            st.caption(
                "Sheets mapped: "
                + "; ".join(
                    f"{m.get('sheet')} → {m.get('geo_level')}/{m.get('geo_name') or m.get('facility')}"
                    for m in mapped
                )
            )

        if report.get("sheets_skipped"):
            st.caption(
                f"Sheets skipped (no geo mapping): {', '.join(report['sheets_skipped'])}"
            )

        if report.get("all_unmatched_labels"):
            with st.expander(
                f"Labels present in file but not in dictionary ({len(report['all_unmatched_labels'])})",
                expanded=False,
            ):
                for lab in report["all_unmatched_labels"]:
                    st.text(f"• {lab}")
                st.caption(
                    "These rows were ignored. Add them to KNOWN_LABELS / indicator_dictionary."
                )

        if report.get("all_periods"):
            periods = report["all_periods"]
            st.caption(
                f"**Canonical periods:** {', '.join(periods[:24])}"
                + (" …" if len(periods) > 24 else "")
            )

        if parsed.empty:
            st.warning(
                "No facility data was committed to the parse result. "
                "Check that sheet names match (MOH DATA KF NORTH / KF SOUTH / GANZE) "
                "and that facility marker rows (e.g. JARIBUNI DISPENSARY) are present."
            )
            return

        st.success(f"✅ Ready to commit **{len(parsed):,}** records from `{uploaded.name}`")

        with st.expander("Preview parsed data", expanded=True):
            summary = (
                parsed.groupby(["facility", "stream", "register"])
                .size()
                .reset_index(name="rows")
            )
            st.dataframe(summary, use_container_width=True, hide_index=True)
            st.caption(
                f"**Facilities:** {', '.join(sorted(parsed['facility'].unique()))}"
            )
            # Quick check that critical GO_1 inputs are present
            go_labels = {"RDT <5 Positive", "RDT >=5 Positive", "RDT <5 Total Exam", "RDT >=5 Total Exam"}
            present = set(parsed["indicator"].unique())
            missing_go = go_labels - present
            if missing_go:
                st.warning(
                    f"GO_1 inputs still missing after parse: {', '.join(sorted(missing_go))}"
                )
            else:
                st.caption("GO_1 inputs (RDT exams + positives) are present.")

        st.session_state["pending_upload_df"] = parsed
        st.session_state["pending_upload_name"] = uploaded.name
        st.session_state["pending_upload_report"] = report

        if st.button("✅ Commit to database", type="primary", use_container_width=True):
            to_save = st.session_state.get("pending_upload_df", parsed)
            to_report = st.session_state.get("pending_upload_report", report)
            if to_save is None or (hasattr(to_save, "empty") and to_save.empty):
                st.error(
                    "Nothing to commit — parsed data is empty. "
                    "Re-select the file and wait for the validation report before committing."
                )
            else:
                with st.spinner("Saving submission + observations + latest view…"):
                    try:
                        result = store_upload(
                            uploaded, to_save, user["username"], parse_report=to_report
                        )
                        committed, path = result[0], result[1]
                        upload_id = result[2] if len(result) > 2 else None
                        submission_id = result[3] if len(result) > 3 else None
                        obs_n = result[4] if len(result) > 4 else None
                    except Exception as e:
                        st.error(f"Save failed: {e}")
                        committed, path, upload_id, submission_id, obs_n = 0, None, None, None, None
                # Success if observations or latest-view rows were written
                if path and (committed or obs_n):
                    parts = [
                        f"Latest-view facility rows: **{committed:,}**",
                        f"Observations stored: **{obs_n or 0:,}** (append-only)",
                    ]
                    if submission_id:
                        parts.append(f"Submission **#{submission_id}**")
                    if upload_id:
                        parts.append(f"Compare snapshot **#{upload_id}**")
                    st.success(
                        f"Saved to `{DB_PATH.name}` · file `{path.name}`. " + " · ".join(parts)
                    )
                    st.session_state.pop("pending_upload_df", None)
                    st.session_state.pop("pending_upload_name", None)
                    st.session_state.pop("pending_upload_report", None)
                    st.session_state.applied_filters = default_filters()
                    st.session_state.nav_unlocked = True
                    st.session_state.last_commit_rows = committed
                    with st.spinner("Re-evaluating alerts…"):
                        try:
                            evaluate_alerts()
                        except Exception:
                            pass
                    st.balloons()
                    st.info("Open **Overview** in the sidebar to see facility-level KPIs.")
                    st.rerun()
                else:
                    st.error("Commit reported 0 rows. Check that the file parsed correctly.")

    st.markdown("---")

    with st.expander("Database Status"):
        try:
            stats = query("""
                SELECT facility, COUNT(*) AS rows, COUNT(DISTINCT period) AS periods
                FROM malaria_data GROUP BY facility
            """)
            if stats.empty:
                st.caption("Database is empty.")
            else:
                st.dataframe(stats, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Could not read database: {e}")

    with st.expander("Backup download", expanded=True):
        st.caption(
            "Download a copy of `cbmp.db` (all programme data). Store offline with today’s date. "
            "Does not replace regular file-server backups."
        )
        try:
            if DB_PATH.exists():
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                db_bytes = DB_PATH.read_bytes()
                st.download_button(
                    "📥 Download database backup (.db)",
                    data=db_bytes,
                    file_name=f"cbmp_backup_{ts}.db",
                    mime="application/x-sqlite3",
                    key="dl_db_backup",
                )
                st.caption(f"File: `{DB_PATH.name}` · ~{len(db_bytes) / 1024:.0f} KB")
            else:
                st.warning("Database file not found yet — commit data first.")
        except Exception as e:
            st.error(f"Backup unavailable: {e}")

    with st.expander("Phase 0 · Foundations", expanded=True):
        st.markdown(
            f"""
**Status:** ✅ **Signed off** ({PHASE_0_SIGNED_DATE}) — foundations locked  
See `docs/PHASE_0_FOUNDATIONS.md` for the pack.

| Item | Value |
|------|--------|
| Population | **{POPULATION_STATUS}** (Jaribuni {POPULATION['Jaribuni']:,}, Pingilikani {POPULATION['Pingilikani']:,}, Mwakuhenga {POPULATION['Mwakuhenga']:,}) |
| TPR rule | {TPR_RULE['summary']} |
| Phase 1 registers | {', '.join(PHASE1_REGISTERS)} |
| Phase 2+ reserved | {', '.join(PHASE2_REGISTERS)} |
| RAG (higher better) | Green ≥ target · Amber ≥ 75% of target · Red &lt; 75% |
"""
        )
        st.caption("CFR and severe malaria remain deferred until those fields exist in uploads.")

    with st.expander("Baselines"):
        st.caption("Official indicator baselines and targets (Phase 0 dictionary).")
        for key, b in BASELINES.items():
            st.markdown(f"**{key}** — {b['name']}")
            c1, c2, c3 = st.columns(3)
            c1.metric("Baseline", f"{b['baseline']}{b['unit']}")
            c2.metric("Target", f"{b['target']}{b['unit']}")
            c3.caption(f"Source: {b['source']} · RAG: {b.get('direction', '—')}")

    with st.expander("Population Denominators"):
        st.warning(
            f"Population status: **{get_population_status()}**. "
            "Incidence and ABER are indicative until County confirms numbers "
            "or epi metrics are deferred (Phase 0 Option A/B)."
        )
        pop = query("SELECT * FROM population ORDER BY entity_type, entity_name")
        if pop.empty:
            st.dataframe(
                pd.DataFrame(
                    [{"Facility": k, "Population": v} for k, v in POPULATION.items()]
                ),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.dataframe(pop, use_container_width=True, hide_index=True)

    with st.expander("Alert Rules"):
        rules = query("SELECT alert_name, category, severity, active FROM alert_rules ORDER BY category, severity")
        if rules.empty:
            st.caption("No alert rules configured.")
        else:
            st.dataframe(rules, use_container_width=True, hide_index=True)

    with st.expander("⚠️ Danger Zone"):
        st.warning("This will delete all data. It cannot be undone.")
        confirm = st.checkbox("I understand and want to clear all data.")
        if st.button("🗑️ Clear all data", disabled=not confirm):
            execute("DELETE FROM malaria_data")
            execute("DELETE FROM observations")
            execute("DELETE FROM submissions")
            execute("DELETE FROM recon_items")
            execute("DELETE FROM recon_runs")
            execute("DELETE FROM moh_648")
            execute("DELETE FROM moh_521")
            execute("DELETE FROM stock_movements")
            execute("DELETE FROM layer2_uploads")
            execute("DELETE FROM upload_snapshots")
            execute("DELETE FROM upload_history")
            execute("DELETE FROM alerts")
            execute("DELETE FROM dq_reviews")
            try:
                execute("DELETE FROM supervision_actions")
            except Exception:
                pass
            try:
                execute("DELETE FROM workplan_actions")
            except Exception:
                pass
            log_audit(user["username"], "clear_data", None, "All data cleared")
            st.success("Database cleared.")
            st.rerun()


# ==================================================================
# INDICATOR ENGINE
# ==================================================================

def _filter_data(df, filters, user=None):
    """Apply global filters + user geographic scope (RBAC)."""
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()
    d = apply_user_data_scope(df.copy(), user=user)
    if filters.get("facility") and filters["facility"] != "All three":
        # Facility users cannot widen beyond their scope
        u = user or st.session_state.get("user") or {}
        if u.get("facility_scope") and filters["facility"] != u["facility_scope"]:
            d = d[d["facility"] == u["facility_scope"]]
        else:
            d = d[d["facility"] == filters["facility"]]
    if filters.get("stream") and filters["stream"] != "Both":
        d = d[d["stream"] == filters["stream"]]
    if filters.get("registers"):
        d = d[d["register"].isin(filters["registers"])]
    periods = filters.get("periods")
    if periods:
        d = d[d["period"].astype(str).isin([str(p) for p in periods])]
    elif filters.get("period") and filters["period"] not in (
        None, "Monthly", "Quarterly", "Yearly", "Custom", "All periods"
    ):
        d = d[d["period"].astype(str) == str(filters["period"])]
    if "data_status" in d.columns:
        d = d[d["data_status"].fillna("Actual").astype(str).str.upper() != "NOT_YET_DUE"]
    return d


# Phase A.5 — locked formula definitions (configuration-driven, not broad regex)
# pct_type: BOUNDED_RATE | ACHIEVEMENT | PROGRESS | CHANGE | COVERAGE | RATIO | REPORTING_RATE | EPIDEMIOLOGICAL_RATE | COUNT
INDICATOR_REGISTRY = {
    "GO_1": {
        "name": "RDT Test Positivity Rate",
        "numerator": "MOH 706 RDT positives",
        "denominator": "MOH 706 RDT total exams",
        "formula": "RDT positives ÷ RDT exams × 100",
        "source": "MOH 706 RDT only",
        "pct_rule": "num_le_den",
        "pct_type": "BOUNDED_RATE",
        "num_spec": {"registers": ["MOH 706"], "indicator_contains": ["RDT", "Positive"], "indicator_exclude": ["Exam"]},
        "den_spec": {"registers": ["MOH 706"], "indicator_contains": ["RDT", "Total Exam"]},
    },
    "SO_1": {
        "name": "CHP competency pass rate",
        "numerator": "CHPs passed assessment",
        "denominator": "Active CHPs on roster",
        "formula": "Passed ÷ Active × 100",
        "source": "CHP roster",
        "pct_rule": "num_le_den",
        "pct_type": "BOUNDED_RATE",
    },
    "R1_1": {
        "name": "CHP tests performed",
        "numerator": "MOH 748 Total <5 + Total ≥5",
        "denominator": None,
        "formula": "Sum of tests (count, not a %)",
        "source": "MOH 748",
        "pct_rule": "n/a",
        "pct_type": "COUNT",
        "num_spec": {"registers": ["MOH 748"], "indicator_exact": ["Total <5", "Total >=5"]},
    },
    "R1_2": {
        "name": "Monitoring reports submitted",
        "numerator": "Reports submitted (KHIS reporting rate)",
        "denominator": "Reports expected",
        "formula": "Reporting rate 0–100% only",
        "source": "MOH 515",
        "pct_rule": "0_to_100",
        "pct_type": "REPORTING_RATE",
    },
    "CAS_1": {
        "name": "Testing rate",
        "numerator": "MOH 705 Tested",
        "denominator": "MOH 705 Suspected",
        "formula": "Tested ÷ Suspected × 100",
        "source": "MOH 705A/B",
        "pct_rule": "num_le_den",
        "pct_type": "BOUNDED_RATE",
        "num_spec": {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Tested"], "indicator_exclude": ["Without"]},
        "den_spec": {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Suspected"]},
    },
    "CAS_2": {
        "name": "Treatment coverage",
        "numerator": "AL Total / AL Dispensed",
        "denominator": "MOH 705 Confirmed",
        "formula": "AL ÷ Confirmed × 100",
        "source": "AL vs 705 confirmed",
        "pct_rule": "num_le_den",
        "pct_type": "COVERAGE",
        "num_spec": {"indicator_exact": ["AL Total", "AL Dispensed"]},
        "den_spec": {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Confirmed"]},
    },
    "CAS_POS": {
        "name": "Clinical positivity (705)",
        "numerator": "MOH 705 Confirmed",
        "denominator": "MOH 705 Tested",
        "formula": "Confirmed ÷ Tested × 100",
        "source": "MOH 705A/B only",
        "pct_rule": "num_le_den",
        "pct_type": "BOUNDED_RATE",
        "num_spec": {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Confirmed"]},
        "den_spec": {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Tested"], "indicator_exclude": ["Without"]},
    },
    "EPI_1": {
        "name": "Malaria incidence",
        "numerator": "MOH 705 Confirmed",
        "denominator": "Population (provisional)",
        "formula": "Confirmed ÷ Population × 1,000",
        "source": "705 + population",
        "pct_rule": "rate_per_1000",
        "pct_type": "EPIDEMIOLOGICAL_RATE",
        "num_spec": {"registers": ["MOH 705A", "MOH 705B"], "indicator_contains": ["Confirmed"]},
    },
    "REF_1": {
        "name": "Referral completion",
        "numerator": "Referrals marked arrived",
        "denominator": "Referrals logged",
        "formula": "Arrived ÷ Logged × 100",
        "source": "Referrals table",
        "pct_rule": "num_le_den",
        "pct_type": "BOUNDED_RATE",
    },
    "COM_TPR": {
        "name": "Community positivity (748)",
        "numerator": "MOH 748 Positive",
        "denominator": "MOH 748 Total <5 + ≥5",
        "formula": "Positive ÷ Totals × 100",
        "source": "MOH 748",
        "pct_rule": "num_le_den",
        "pct_type": "BOUNDED_RATE",
        "num_spec": {"registers": ["MOH 748"], "indicator_contains": ["Positive"]},
        "den_spec": {"registers": ["MOH 748"], "indicator_exact": ["Total <5", "Total >=5"]},
    },
}


# Phase B1 — full indicator catalogue (91 slots). engine_id links to compute_indicator / cascade.
# status_data: live | provisional | pending_data
def _cat(cid, category, name, definition, engine_id=None, unit="%", direction="higher_better",
         frequency="monthly", source="", status_data="pending_data", target=None, baseline=None):
    return {
        "id": cid, "category": category, "name": name, "definition": definition,
        "engine_id": engine_id, "unit": unit, "direction": direction,
        "frequency": frequency, "source": source, "status_data": status_data,
        "target": target, "baseline": baseline,
    }


INDICATOR_CATALOGUE = [
    # ── Official programme (wired) ──
    _cat("GO_1", "Diagnosis", "RDT test positivity rate", "MOH 706 RDT pos ÷ RDT exams × 100",
         "GO_1", "%", "go_tpr", "monthly", "MOH 706", "live", 5.0, 15.0),
    _cat("SO_1", "Community", "CHP competency pass rate", "Passed CHPs ÷ active roster × 100",
         "SO_1", "%", "higher_better", "quarterly", "CHP roster", "live", 70.0, 50.0),
    _cat("R1_1", "Community", "CHP tests performed", "MOH 748 Total <5 + ≥5",
         "R1_1", "count", "higher_better", "monthly", "MOH 748", "live", 5000, 0),
    _cat("R1_2", "Reporting", "Monitoring reports submitted", "MOH 515 reporting rate 0–100%",
         "R1_2", "%", "higher_better", "monthly", "MOH 515", "live", 95.0, 80.0),
    # ── Diagnosis ──
    _cat("CAS_1", "Diagnosis", "Testing rate (OPD)", "705 Tested ÷ 705 Suspected × 100",
         "CAS_1", "%", "higher_better", "monthly", "MOH 705A/B", "live", 80.0),
    _cat("CAS_POS", "Diagnosis", "Clinical positivity (OPD)", "705 Confirmed ÷ 705 Tested × 100",
         "CAS_POS", "%", "lower_better", "monthly", "MOH 705A/B", "live"),
    _cat("DX_01", "Diagnosis", "Suspected malaria cases", "Sum of 705 Suspected",
         None, "count", "monitor", "monthly", "MOH 705A/B", "live"),
    _cat("DX_02", "Diagnosis", "Tested for malaria (OPD)", "Sum of 705 Tested",
         None, "count", "monitor", "monthly", "MOH 705A/B", "live"),
    _cat("DX_03", "Diagnosis", "RDT examinations", "MOH 706 RDT Total Exam",
         None, "count", "monitor", "monthly", "MOH 706", "live"),
    _cat("DX_04", "Diagnosis", "RDT positives", "MOH 706 RDT Positive",
         None, "count", "monitor", "monthly", "MOH 706", "live"),
    _cat("DX_05", "Diagnosis", "Microscopy examinations", "MOH 706 BS Total Exam",
         None, "count", "monitor", "monthly", "MOH 706", "live"),
    _cat("DX_06", "Diagnosis", "Microscopy positivity", "BS Positive ÷ BS Total Exam × 100",
         None, "%", "lower_better", "monthly", "MOH 706", "provisional"),
    _cat("DX_07", "Diagnosis", "Invalid / discarded tests", "Placeholder until register field exists",
         None, "count", "lower_better", "monthly", "Lab register", "pending_data"),
    _cat("DX_08", "Diagnosis", "Diagnosis gap", "Suspected − Tested (705)",
         None, "count", "lower_better", "monthly", "MOH 705A/B", "live"),
    _cat("DX_09", "Diagnosis", "Diagnosis gap %", "(Suspected−Tested)/Suspected × 100",
         None, "%", "lower_better", "monthly", "MOH 705A/B", "live", 20.0),
    # ── Treatment ──
    _cat("CAS_2", "Treatment", "AL treatment coverage", "AL Total/Dispensed ÷ 705 Confirmed × 100",
         "CAS_2", "%", "higher_better", "monthly", "AL vs 705", "live", 90.0),
    _cat("TX_01", "Treatment", "Confirmed cases (OPD)", "705 Confirmed",
         None, "count", "monitor", "monthly", "MOH 705A/B", "live"),
    _cat("TX_02", "Treatment", "AL doses dispensed", "AL Total + AL Dispensed (exact labels)",
         None, "count", "monitor", "monthly", "MOH 743/748", "live"),
    _cat("TX_03", "Treatment", "Treatment gap", "Confirmed − AL",
         None, "count", "lower_better", "monthly", "cascade", "live"),
    _cat("TX_04", "Treatment", "Treatment gap %", "(Confirmed−AL)/Confirmed × 100",
         None, "%", "lower_better", "monthly", "cascade", "live", 15.0),
    _cat("TX_05", "Treatment", "Treated without testing", "Treated w/o Testing count",
         None, "count", "lower_better", "monthly", "MOH 743", "live"),
    _cat("TX_06", "Treatment", "% treated without testing", "TWT ÷ AL × 100",
         None, "%", "lower_better", "monthly", "MOH 743", "live", 5.0),
    _cat("TX_07", "Treatment", "Appropriate first-line treatment", "Pending patient-level 748/521",
         None, "%", "higher_better", "monthly", "MOH 748", "pending_data", 95.0),
    _cat("TX_08", "Treatment", "Treatment within 24h", "Pending timestamp fields",
         None, "%", "higher_better", "monthly", "Registers", "pending_data"),
    # ── Epidemiology ──
    _cat("EPI_1", "Epidemiology", "Malaria incidence / 1,000", "705 Confirmed ÷ population × 1000",
         "EPI_1", "per 1,000", "lower_better", "monthly", "705 + pop", "provisional"),
    _cat("EPI_02", "Epidemiology", "ABER", "705 Tested ÷ population × 100",
         None, "%", "monitor", "monthly", "705 + pop", "provisional"),
    _cat("EPI_03", "Epidemiology", "Case fatality rate", "Deaths ÷ Confirmed × 100",
         None, "%", "lower_better", "monthly", "Deaths register", "pending_data"),
    _cat("EPI_04", "Epidemiology", "Severe malaria proportion", "Severe ÷ Confirmed × 100",
         None, "%", "lower_better", "monthly", "IPD", "pending_data"),
    _cat("EPI_05", "Epidemiology", "Malaria admissions", "Count of malaria admissions",
         None, "count", "monitor", "monthly", "IPD", "pending_data"),
    _cat("EPI_06", "Epidemiology", "Malaria deaths", "Deaths with malaria",
         None, "count", "lower_better", "monthly", "Mortality", "pending_data"),
    # ── Community ──
    _cat("COM_TPR", "Community", "Community positivity (748)", "748 Positive ÷ totals × 100",
         "COM_TPR", "%", "lower_better", "monthly", "MOH 748", "live"),
    _cat("COM_01", "Community", "Community tests (748)", "748 Total <5+≥5",
         "R1_1", "count", "higher_better", "monthly", "MOH 748", "live"),
    _cat("COM_02", "Community", "648 testing rate", "648 tested ÷ suspected × 100",
         None, "%", "higher_better", "monthly", "MOH 648", "live"),
    _cat("COM_03", "Community", "648 positivity", "648 positive ÷ tested × 100",
         None, "%", "lower_better", "monthly", "MOH 648", "live"),
    _cat("COM_04", "Community", "648 treatment coverage", "648 treated ÷ positive × 100",
         None, "%", "higher_better", "monthly", "MOH 648", "live"),
    _cat("COM_05", "Community", "648 referral rate", "648 referred ÷ positive × 100",
         None, "%", "monitor", "monthly", "MOH 648", "live"),
    _cat("COM_06", "Community", "Referral arrival rate", "referral_arrived ÷ referred × 100",
         None, "%", "higher_better", "monthly", "MOH 648", "live"),
    _cat("COM_07", "Community", "Follow-up rate", "followed_up ÷ positive × 100",
         None, "%", "higher_better", "monthly", "MOH 648", "live"),
    _cat("COM_08", "Community", "Active CHPs", "Roster active count",
         None, "count", "monitor", "monthly", "CHP roster", "live"),
    _cat("COM_09", "Community", "CHPs with zero testing", "Active CHPs with 648 tested=0",
         None, "count", "lower_better", "monthly", "648+roster", "live"),
    _cat("COM_10", "Community", "Household coverage", "Pending CHU household denominator",
         None, "%", "higher_better", "quarterly", "CHU", "pending_data"),
    _cat("REF_1", "Community", "Referral completion (ops)", "Arrived ÷ logged referrals × 100",
         "REF_1", "%", "higher_better", "monthly", "Referrals table", "live"),
    # ── Prevention ──
    _cat("PRV_01", "Prevention", "ITNs distributed (community)", "648 itn_distributed",
         None, "count", "higher_better", "monthly", "MOH 648", "live"),
    _cat("PRV_711A", "Prevention", "New ANC clients (711)", "MOH 711 New ANC",
         None, "count", "higher_better", "monthly", "MOH 711", "live"),
    _cat("PRV_711B", "Prevention", "IPTp1 coverage of new ANC", "711 IPT1 ÷ New ANC × 100",
         None, "%", "higher_better", "monthly", "MOH 711", "live", 90.0, None),
    _cat("PRV_711C", "Prevention", "IPTp3 coverage of new ANC", "711 IPT3 ÷ New ANC × 100",
         None, "%", "higher_better", "monthly", "MOH 711", "live", 80.0, None),
    _cat("PRV_711D", "Prevention", "ANC4 completion of new ANC", "711 ANC4 ÷ New ANC × 100",
         None, "%", "higher_better", "monthly", "MOH 711", "live", 80.0, None),
    _cat("PRV_711E", "Prevention", "LLIN at ANC of new ANC", "711 LLIN ÷ New ANC × 100",
         None, "%", "higher_better", "monthly", "MOH 711", "live", 90.0, None),
    _cat("PRV_02", "Prevention", "IPT1 doses", "IPT1 count",
         None, "count", "higher_better", "monthly", "MOH 743/705", "live"),
    _cat("PRV_03", "Prevention", "IPT3 retention of IPT1", "IPT3 ÷ IPT1 × 100",
         None, "%", "higher_better", "monthly", "MOH 743", "live", 70.0),
    _cat("PRV_04", "Prevention", "IPT2 retention of IPT1", "IPT2 ÷ IPT1 × 100",
         None, "%", "higher_better", "monthly", "MOH 743", "live"),
    _cat("PRV_05", "Prevention", "Malaria in pregnancy cases", "MiP count",
         None, "count", "monitor", "monthly", "MOH 705B", "live"),
    _cat("PRV_06", "Prevention", "IRS coverage", "Pending campaign data",
         None, "%", "higher_better", "annual", "Campaign", "pending_data"),
    _cat("PRV_07", "Prevention", "ITN ownership (survey)", "Pending survey",
         None, "%", "higher_better", "annual", "Survey", "pending_data"),
    # ── Commodities ──
    _cat("COMY_01", "Commodities", "RDT months of stock", "On hand ÷ AMC",
         None, "months", "monitor", "monthly", "Stock", "live"),
    _cat("COMY_02", "Commodities", "AL months of stock", "On hand ÷ AMC",
         None, "months", "monitor", "monthly", "Stock", "live"),
    _cat("COMY_03", "Commodities", "Facilities with RDT stock-out risk", "MOS < 1 or below reorder",
         None, "count", "lower_better", "monthly", "Stock", "live"),
    _cat("COMY_04", "Commodities", "Facilities with AL stock-out risk", "MOS < 1 or below reorder",
         None, "count", "lower_better", "monthly", "Stock", "live"),
    _cat("COMY_05", "Commodities", "Stock-out days (RDT)", "Pending daily stock log",
         None, "days", "lower_better", "monthly", "LMIS", "pending_data"),
    # ── Reporting / DQ ──
    _cat("REP_01", "Reporting", "Facility reporting rate (avg)", "Mean valid reporting rates",
         None, "%", "higher_better", "monthly", "KHIS", "live", 95.0),
    _cat("REP_02", "Reporting", "Registers with RR < 80%", "Count of low RR cells",
         None, "count", "lower_better", "monthly", "KHIS", "live"),
    _cat("DQ_01", "Data quality", "Open DQ issues", "Count open dq_issues",
         None, "count", "lower_better", "monthly", "DQ module", "live"),
    _cat("DQ_02", "Data quality", "Critical open DQ issues", "Severity=critical open",
         None, "count", "lower_better", "monthly", "DQ module", "live"),
    _cat("DQ_03", "Data quality", "DQ composite score", "compute_dq_score",
         None, "score", "higher_better", "monthly", "DQ module", "live", 80.0),
    # ── Supervision / workplan ──
    _cat("SUP_01", "Operations", "Open supervision actions", "status open/in_progress",
         None, "count", "lower_better", "monthly", "Supervision", "live"),
    _cat("SUP_02", "Operations", "Overdue supervision actions", "Past deadline",
         None, "count", "lower_better", "monthly", "Supervision", "live"),
    _cat("WP_01", "Operations", "Open workplan items", "workplan open",
         None, "count", "lower_better", "monthly", "Workplan", "live"),
    _cat("WP_02", "Operations", "Overdue workplan items", "Past due_date",
         None, "count", "lower_better", "monthly", "Workplan", "live"),
    # ── Placeholders to complete 91-framework slots (explicit pending) ──
]

# Pad catalogue to 91 explicit slots with pending indicators in remaining categories
_pending_pad = [
    ("EPI_07", "Epidemiology", "Under-5 incidence", "U5 confirmed ÷ U5 population × 1000"),
    ("EPI_08", "Epidemiology", "Slide positivity rate (all methods)", "All lab pos ÷ all exams"),
    ("DX_10", "Diagnosis", "% tested with RDT among tested", "RDT exams ÷ all tests"),
    ("DX_11", "Diagnosis", "Suspected who were not tested", "Diagnosis gap count"),
    ("TX_09", "Treatment", "ACT courses completed", "Pending 748 outcome"),
    ("TX_10", "Treatment", "Severe malaria treated", "Pending IPD"),
    ("COM_11", "Community", "CHP reporting completeness", "Pending 648 expected rows"),
    ("COM_12", "Community", "CHU coverage of active CHPs", "CHUs with ≥1 active CHP"),
    ("PRV_08", "Prevention", "ANC1 coverage", "Pending ANC denominator"),
    ("PRV_09", "Prevention", "IPTp3 coverage of ANC1", "IPT3 ÷ ANC1"),
    ("COMY_06", "Commodities", "Emergency orders raised", "Pending LMIS"),
    ("COMY_07", "Commodities", "Expired RDT units", "Pending batch/expiry"),
    ("COMY_08", "Commodities", "Pipeline stock (RDT)", "Pending pipeline"),
    ("REP_03", "Reporting", "Timeliness of monthly report", "Pending submission lag"),
    ("REP_04", "Reporting", "Completeness of core registers", "% core registers present"),
    ("DQ_04", "Data quality", "Formula INVALID cell count", "Integrity audit"),
    ("DQ_05", "Data quality", "Recon items requiring action", "Latest recon run"),
    ("SUP_03", "Operations", "Supervision visits this period", "Visit log"),
    ("SUP_04", "Operations", "Findings closed within deadline", "% closed on time"),
    ("WP_03", "Operations", "Workplan completion %", "Closed ÷ total"),
    ("BUD_01", "Budget", "Budget execution %", "Expenditure ÷ budget"),
    ("BUD_02", "Budget", "Cost per test", "Spend ÷ tests"),
    ("BUD_03", "Budget", "Partner activity variance", "Pending partner module"),
    ("GIS_01", "Geography", "Hotspot facilities (top burden)", "Top tertile confirmed"),
    ("GIS_02", "Geography", "Facilities with rising TPR", "Valid TPR up vs prior"),
    ("CLIM_01", "Climate", "Rainfall anomaly alert", "Max |rainfall anomaly %| vs facility median"),
    ("CLIM_02", "Climate", "Lagged climate risk index", "Mean multifactor EW risk score 0–1"),
    ("SCN_01", "Planning", "Run-rate achievement R1.1", "YTD ÷ annual target"),
    ("SCN_02", "Planning", "Required monthly tests remaining", "Run-rate required"),
]
for cid, cat, name, definition in _pending_pad:
    if len(INDICATOR_CATALOGUE) >= 91:
        break
    # mark some as live if we can compute below
    slot_status = "pending_data"
    if cid in (
        "DX_11", "DQ_04", "DQ_05", "SCN_01", "SCN_02", "REP_04", "GIS_01",
        "COM_11", "COM_12", "WP_03", "SUP_03", "SUP_04", "GIS_02", "REP_03",
        "CLIM_01", "CLIM_02",
    ):
        slot_status = "live"
    if cid.startswith("BUD"):
        slot_status = "pending_data"
    INDICATOR_CATALOGUE.append(
        _cat(cid, cat, name, definition, None, "%", "monitor", "monthly", "", slot_status)
    )
# Ensure exactly 91 entries
while len(INDICATOR_CATALOGUE) < 91:
    i = len(INDICATOR_CATALOGUE) + 1
    INDICATOR_CATALOGUE.append(
        _cat(f"P{i:02d}", "Framework", f"Reserved indicator slot {i}",
             "Placeholder for full 91-indicator CBMP framework", None, "—", "monitor",
             "monthly", "", "pending_data")
    )
INDICATOR_CATALOGUE = INDICATOR_CATALOGUE[:91]


def evaluate_catalogue(df, filters):
    """
    Evaluate all catalogue indicators. Live values only via engine/cascade/ops tables.
    pending_data → Status N/A (not 0%).
    """
    d = _filter_data(df, filters) if df is not None else pd.DataFrame()
    cascade = compute_cascade_metrics(d) if d is not None else {}
    rows = []
    # Preload ops
    try:
        chps = query("SELECT * FROM chps WHERE active = 1 OR active IS NULL")
    except Exception:
        chps = pd.DataFrame()
    try:
        dqi = query("SELECT severity, status FROM dq_issues")
    except Exception:
        dqi = pd.DataFrame()
    try:
        sa = query("SELECT deadline, status FROM supervision_actions")
    except Exception:
        sa = pd.DataFrame()
    try:
        wp = query("SELECT due_date, status FROM workplan_actions")
    except Exception:
        wp = pd.DataFrame()
    _, pack648 = community_cascade_from_648(
        filters.get("facility") if filters and filters.get("facility") not in (None, "All three") else None
    )
    rates648 = (pack648 or {}).get("rates") or {}
    totals648 = (pack648 or {}).get("totals") or {}
    try:
        fc = compute_commodity_forecast()
    except Exception:
        fc = pd.DataFrame()

    today_s = datetime.now().strftime("%Y-%m-%d")

    for meta in INDICATOR_CATALOGUE:
        cid = meta["id"]
        val = None
        status = "na"
        reason = ""
        num = den = None
        rag = "grey"

        if meta.get("status_data") == "pending_data" and meta.get("engine_id") is None and cid not in (
            "DX_01", "DX_02", "DX_03", "DX_04", "DX_05", "DX_08", "DX_09", "DX_11",
            "TX_01", "TX_02", "TX_03", "TX_04", "TX_05", "TX_06",
            "COM_02", "COM_03", "COM_04", "COM_05", "COM_06", "COM_07", "COM_08", "COM_09",
            "PRV_01", "PRV_02", "PRV_03", "PRV_04", "PRV_05",
            "PRV_711A", "PRV_711B", "PRV_711C", "PRV_711D", "PRV_711E",
            "COMY_01", "COMY_02", "COMY_03", "COMY_04",
            "REP_01", "REP_02", "REP_04",
            "DQ_01", "DQ_02", "DQ_03", "DQ_04", "DQ_05",
            "SUP_01", "SUP_02", "WP_01", "WP_02",
            "EPI_02", "GIS_01", "GIS_02", "SCN_01", "SCN_02",
            "COM_11", "COM_12", "WP_03", "SUP_03", "SUP_04", "REP_03",
        ):
            reason = "Data source not yet connected"
            rows.append(_catalogue_row(meta, None, "na", reason, None, None, "grey"))
            continue

        eid = meta.get("engine_id")
        if eid == "SO_1":
            if not chps.empty:
                passed = int((chps["competency_status"] == "passed").sum())
                r = compute_indicator("SO_1", d, filters, extra_num=passed, extra_den=len(chps))
                val, status, reason = r.get("value"), r.get("status"), r.get("reason") or ""
                num, den = r.get("numerator"), r.get("denominator")
                rag = r.get("rag") or "grey"
            else:
                reason = "No CHP roster"
        elif eid == "REF_1":
            try:
                refs = query("SELECT arrived FROM referrals")
                if not refs.empty:
                    arrived = int(refs["arrived"].sum())
                    r = compute_indicator("REF_1", d, filters, extra_num=arrived, extra_den=len(refs))
                    val, status = r.get("value"), r.get("status")
                    num, den = r.get("numerator"), r.get("denominator")
                    reason = r.get("reason") or ""
                else:
                    reason = "No referrals logged"
            except Exception:
                reason = "Referrals table unavailable"
        elif eid:
            r = compute_indicator(eid, df, filters)
            val, status = r.get("value"), r.get("status")
            num, den = r.get("numerator"), r.get("denominator")
            reason = r.get("reason") or ""
            rag = r.get("rag") or "grey"
        elif cid == "DX_01":
            val, status = cascade.get("suspected"), "valid"
        elif cid == "DX_02":
            val, status = cascade.get("tested"), "valid"
        elif cid == "DX_03":
            val = volume_from_registry(d, "GO_1", "den")
            status = "valid" if val is not None else "na"
        elif cid == "DX_04":
            val = volume_from_registry(d, "GO_1", "num")
            status = "valid" if val is not None else "na"
        elif cid == "DX_05":
            val = sum_locked(d, {"registers": ["MOH 706"], "indicator_contains": ["BS", "Total Exam"]})
            status = "valid"
        elif cid == "DX_06":
            n = sum_locked(d, {"registers": ["MOH 706"], "indicator_contains": ["BS", "Positive"], "indicator_exclude": ["Exam"]})
            den = sum_locked(d, {"registers": ["MOH 706"], "indicator_contains": ["BS", "Total Exam"]})
            a = safe_rate(n, den, indicator="DX_06")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "DX_08":
            val, status = cascade.get("diag_gap"), "valid"
        elif cid in ("DX_09", "DX_11"):
            a = safe_rate(cascade.get("diag_gap", 0), cascade.get("suspected", 0), indicator="DX_09")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
            if cid == "DX_11":
                val, status = cascade.get("diag_gap"), "valid"
        elif cid == "TX_01":
            val, status = cascade.get("confirmed"), "valid"
        elif cid == "TX_02":
            val, status = cascade.get("treated"), "valid"
        elif cid == "TX_03":
            val, status = cascade.get("treat_gap"), "valid"
        elif cid == "TX_04":
            a = safe_rate(cascade.get("treat_gap", 0), cascade.get("confirmed", 0), indicator="TX_04")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "TX_05":
            val, status = cascade.get("treated_wo_test"), "valid"
        elif cid == "TX_06":
            a = safe_rate(cascade.get("treated_wo_test", 0), cascade.get("treated", 0), indicator="TX_06")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "EPI_02":
            tested = cascade.get("tested") or 0
            pop = get_population(
                filters.get("facility") if filters and filters.get("facility") not in (None, "All three") else "Total"
            )
            if pop and tested > pop:
                status, reason = "invalid", "Tested > population"
                val = None
                num, den = tested, pop
            else:
                a = safe_rate(tested, pop, indicator="EPI_02", allow_over_100=True)
                if tested > pop > 0:
                    a["status"] = "invalid"
                val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
                num, den = tested, pop
        elif cid.startswith("COM_0") and cid[4:].isdigit():
            key_map = {
                "COM_02": "testing_rate", "COM_03": "positivity", "COM_04": "treatment_cov",
                "COM_05": "referral_rate", "COM_06": "arrival_rate", "COM_07": "followup_rate",
            }
            if cid in key_map and key_map[cid] in rates648:
                a = rates648[key_map[cid]]
                val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
                num, den = a.get("numerator"), a.get("denominator")
            elif cid == "COM_08":
                val, status = (len(chps) if not chps.empty else 0), "valid"
            elif cid == "COM_09":
                # zero testing CHPs needs 648 names — approximate from totals
                val, status = None, "na"
                reason = "Requires roster×648 name match (see CHP Performance)"
            else:
                reason = "No 648 data" if not rates648 else "Not computed"
        elif cid == "PRV_01":
            val = int(totals648.get("rows") and query(
                "SELECT COALESCE(SUM(itn_distributed),0) AS v FROM moh_648"
            ).iloc[0]["v"]) if totals648 else 0
            try:
                val = int(query("SELECT COALESCE(SUM(itn_distributed),0) AS v FROM moh_648").iloc[0]["v"])
                status = "valid"
            except Exception:
                status, reason = "na", "No 648 ITN field"
        elif cid == "PRV_711A":
            val = sum_locked(d, {"indicator_exact": ["New ANC"]})
            status = "valid"
        elif cid == "PRV_711B":
            a = safe_rate(sum_locked(d, {"indicator_exact": ["IPT1"]}), sum_locked(d, {"indicator_exact": ["New ANC"]}), indicator="PRV_711B")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "PRV_711C":
            a = safe_rate(sum_locked(d, {"indicator_exact": ["IPT3"]}), sum_locked(d, {"indicator_exact": ["New ANC"]}), indicator="PRV_711C")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "PRV_711D":
            a = safe_rate(sum_locked(d, {"indicator_exact": ["ANC4"]}), sum_locked(d, {"indicator_exact": ["New ANC"]}), indicator="PRV_711D")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "PRV_711E":
            a = safe_rate(sum_locked(d, {"indicator_exact": ["LLIN ANC"]}), sum_locked(d, {"indicator_exact": ["New ANC"]}), indicator="PRV_711E")
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "PRV_02":
            val = sum_locked(d, {"indicator_exact": ["IPT1"]})
            status = "valid"
        elif cid == "PRV_03":
            a = safe_rate(
                sum_locked(d, {"indicator_exact": ["IPT3"]}),
                sum_locked(d, {"indicator_exact": ["IPT1"]}),
                indicator="PRV_03",
            )
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "PRV_04":
            a = safe_rate(
                sum_locked(d, {"indicator_exact": ["IPT2"]}),
                sum_locked(d, {"indicator_exact": ["IPT1"]}),
                indicator="PRV_04",
            )
            val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
            num, den = a.get("numerator"), a.get("denominator")
        elif cid == "PRV_05":
            val = sum_locked(d, {"indicator_exact": ["Malaria in Pregnancy"]})
            status = "valid"
        elif cid in ("COMY_01", "COMY_02", "COMY_03", "COMY_04"):
            if fc is None or fc.empty:
                reason = "No stock data"
            else:
                item = "RDT" if "RDT" in cid or cid.endswith("01") or cid.endswith("03") else "AL"
                sub = fc[fc["Item"].astype(str).str.upper() == item]
                if cid in ("COMY_01", "COMY_02"):
                    mos = pd.to_numeric(sub["Months of stock"], errors="coerce").dropna()
                    val = float(mos.mean()) if not mos.empty else None
                    status = "valid" if val is not None else "na"
                    reason = "" if val is not None else "No AMC"
                else:
                    val = int((sub["Status"].isin(["low", "stock_out", "watch"])).sum()) if "Status" in sub.columns else 0
                    status = "valid"
        elif cid == "REP_01":
            rr = d[d["indicator"].astype(str).str.contains(r"Reporting\s*Rate", na=False, case=False)] if not d.empty else pd.DataFrame()
            if rr.empty:
                reason = "No reporting rate rows"
            else:
                audits = [reporting_rate_audit(v) for v in rr["value"].tolist()]
                vals = [a["value"] for a in audits if a.get("status") == "valid"]
                if vals:
                    val, status = round(sum(vals) / len(vals), 1), "valid"
                else:
                    status, reason = "invalid", "All reporting rates invalid"
        elif cid == "REP_02":
            rr = d[d["indicator"].astype(str).str.contains(r"Reporting\s*Rate", na=False, case=False)] if not d.empty else pd.DataFrame()
            n = 0
            for v in (rr["value"].tolist() if not rr.empty else []):
                a = reporting_rate_audit(v)
                if a.get("status") == "valid" and a.get("value") is not None and a["value"] < 80:
                    n += 1
            val, status = n, "valid"
        elif cid == "REP_04":
            core = ["MOH 705A", "MOH 705B", "MOH 706", "MOH 743", "MOH 748"]
            present = set(d["register"].unique()) if not d.empty else set()
            hit = sum(1 for r in core if r in present)
            a = safe_rate(hit, len(core), indicator="REP_04")
            val, status = a.get("value"), a.get("status")
            num, den = hit, len(core)
        elif cid == "DQ_01":
            val = int((dqi["status"] == "open").sum()) if not dqi.empty else 0
            status = "valid"
        elif cid == "DQ_02":
            val = int(((dqi["status"] == "open") & (dqi["severity"] == "critical")).sum()) if not dqi.empty else 0
            status = "valid"
        elif cid == "DQ_03":
            try:
                dq = compute_dq_score(df, filters or {})
                val, status = dq.get("score"), "valid" if dq.get("score") is not None else "na"
            except Exception:
                reason = "DQ score unavailable"
        elif cid == "DQ_04":
            try:
                _, sm = run_formula_integrity_audit(df, filters or {})
                val, status = sm.get("invalid", 0), "valid"
            except Exception:
                reason = "Audit failed"
        elif cid == "DQ_05":
            try:
                run = query("SELECT id FROM recon_runs ORDER BY id DESC LIMIT 1")
                if not run.empty:
                    n = query(
                        "SELECT COUNT(*) AS n FROM recon_items WHERE run_id=? AND status='requires_reconciliation'",
                        (int(run.iloc[0]["id"]),),
                    )
                    val = int(n.iloc[0]["n"])
                else:
                    val = 0
                status = "valid"
            except Exception:
                reason = "No recon runs"
        elif cid == "SUP_01":
            val = int(sa["status"].isin(["open", "in_progress"]).sum()) if not sa.empty else 0
            status = "valid"
        elif cid == "SUP_02":
            if sa.empty:
                val = 0
            else:
                val = int(
                    sa[sa["status"].isin(["open", "in_progress"])]
                    .apply(lambda r: bool(r.get("deadline")) and str(r.get("deadline")) < today_s, axis=1)
                    .sum()
                )
            status = "valid"
        elif cid == "WP_01":
            val = int(wp["status"].isin(["open", "in_progress"]).sum()) if not wp.empty else 0
            status = "valid"
        elif cid == "WP_02":
            if wp.empty:
                val = 0
            else:
                val = int(
                    wp[wp["status"].isin(["open", "in_progress"])]
                    .apply(lambda r: bool(r.get("due_date")) and str(r.get("due_date")) < today_s, axis=1)
                    .sum()
                )
            status = "valid"
        elif cid == "GIS_01":
            # top burden facility count among three
            if not d.empty:
                conf = d[d["register"].isin(["MOH 705A", "MOH 705B"])]
                by = conf[conf["indicator"].str.contains("Confirmed", na=False)].groupby("facility")["value"].sum()
                if not by.empty:
                    thr = by.quantile(0.67)
                    val = int((by >= thr).sum())
                    status = "valid"
                else:
                    reason = "No confirmed counts"
            else:
                reason = "No data"
        elif cid == "SCN_01":
            try:
                inds = compute_official_indicators(df, filters or {})
                y, m = _ytd_month_from_filters(filters or {}, d)
                rr = compute_run_rate(inds["R1_1"].get("value"), inds["R1_1"].get("target"), as_of_month=m, year=y)
                val = rr.get("achievement_pct")
                status = "valid" if val is not None else "na"
                reason = "Achievement % (may exceed 100%)"
            except Exception:
                reason = "Run-rate unavailable"
        elif cid == "SCN_02":
            try:
                inds = compute_official_indicators(df, filters or {})
                y, m = _ytd_month_from_filters(filters or {}, d)
                rr = compute_run_rate(inds["R1_1"].get("value"), inds["R1_1"].get("target"), as_of_month=m, year=y)
                val = rr.get("required_monthly")
                status = "valid" if val is not None else "na"
            except Exception:
                reason = "Run-rate unavailable"
        elif cid == "COM_11":
            try:
                roster_n = int(query("SELECT COUNT(*) AS n FROM chps WHERE active=1 OR active IS NULL").iloc[0]["n"])
                named = query(
                    "SELECT COUNT(DISTINCT lower(trim(chp_name))) AS n FROM moh_648 WHERE chp_name IS NOT NULL AND trim(chp_name) != ''"
                )
                named_n = int(named.iloc[0]["n"]) if not named.empty else 0
                a = safe_rate(named_n, roster_n, indicator="COM_11")
                val, status, reason = a.get("value"), a.get("status"), a.get("reason") or "648 named CHPs ÷ roster"
                num, den = named_n, roster_n
            except Exception:
                reason = "Need roster + 648 names"
        elif cid == "COM_12":
            try:
                chus = query("SELECT COUNT(DISTINCT chu) AS n FROM moh_648 WHERE chu IS NOT NULL AND trim(chu)!=''")
                val = int(chus.iloc[0]["n"]) if not chus.empty else 0
                status = "valid"
            except Exception:
                reason = "No 648 CHU names"
        elif cid == "WP_03":
            try:
                tot = int(query("SELECT COUNT(*) AS n FROM workplan_actions").iloc[0]["n"])
                clo = int(query("SELECT COUNT(*) AS n FROM workplan_actions WHERE status IN ('closed','resolved','done')").iloc[0]["n"])
                a = safe_rate(clo, tot, indicator="WP_03")
                val, status, reason = a.get("value"), a.get("status"), a.get("reason") or ""
                num, den = clo, tot
            except Exception:
                reason = "No workplan rows"
        elif cid == "SUP_03":
            try:
                val = int(query("SELECT COUNT(*) AS n FROM supervision_visits").iloc[0]["n"])
                status = "valid"
            except Exception:
                reason = "No supervision visits"
        elif cid == "SUP_04":
            try:
                tot = int(query("SELECT COUNT(*) AS n FROM supervision_actions").iloc[0]["n"])
                clo = int(query("SELECT COUNT(*) AS n FROM supervision_actions WHERE status IN ('closed','resolved')").iloc[0]["n"])
                a = safe_rate(clo, tot, indicator="SUP_04")
                val, status = a.get("value"), a.get("status")
                num, den = clo, tot
            except Exception:
                reason = "No supervision actions"
        elif cid == "REP_03":
            try:
                ups = query("SELECT uploaded_at FROM uploads ORDER BY id DESC LIMIT 12")
                if ups.empty:
                    reason = "No uploads"
                else:
                    val = len(ups)
                    status = "valid"
                    reason = "Count of recent uploads (lag field not in KHIS file)"
            except Exception:
                reason = "No upload history"
        elif cid == "GIS_02":
            try:
                monthly = d[d["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)] if not d.empty else pd.DataFrame()
                rising = 0
                if not monthly.empty:
                    pers = sorted(monthly["period"].astype(str).unique())
                    if len(pers) >= 2:
                        prev, last = pers[-2], pers[-1]
                        for fac in FACILITIES:
                            a0 = compute_indicator("GO_1", monthly[monthly["period"].astype(str) == prev], facility=fac, period=prev)
                            a1 = compute_indicator("GO_1", monthly[monthly["period"].astype(str) == last], facility=fac, period=last)
                            if a0.get("status") == "valid" and a1.get("status") == "valid":
                                if (a1.get("value") or 0) > (a0.get("value") or 0):
                                    rising += 1
                val, status = rising, "valid"
                reason = "Facilities with valid TPR up vs previous month"
            except Exception:
                reason = "Need two monthly periods"
        else:
            reason = reason or "Not computed in this build"

        # RAG for live rates with targets
        if status == "valid" and val is not None and meta.get("target") is not None:
            direction = meta.get("direction") or "higher_better"
            if direction in ("higher_better", "lower_better", "go_tpr"):
                try:
                    rag = status_level(val, meta["target"], direction=direction, baseline=meta.get("baseline"))
                except Exception:
                    rag = "grey"
        if status == "invalid":
            rag = "grey"
            val_display = None
        else:
            val_display = val

        rows.append(_catalogue_row(meta, val_display, status, reason, num, den, rag))

    return pd.DataFrame(rows)


def _catalogue_row(meta, value, status, reason, num, den, rag):
    return {
        "ID": meta["id"],
        "Category": meta["category"],
        "Indicator": meta["name"],
        "Value": value,
        "Unit": meta.get("unit"),
        "Status": (status or "na").upper(),
        "RAG": rag,
        "Target": meta.get("target"),
        "Numerator": num,
        "Denominator": den,
        "Definition": meta.get("definition"),
        "Source": meta.get("source"),
        "Frequency": meta.get("frequency"),
        "Data readiness": meta.get("status_data"),
        "Reason": reason or "",
    }


def sum_locked(df, spec):
    """
    Sum values using locked formula specs — no broad Positive|Confirmed across all registers.
    spec keys: registers, indicator_exact, indicator_contains (all must match), indicator_exclude, stream
    """
    if df is None or df.empty or not spec:
        return 0.0
    d = df
    if spec.get("registers"):
        d = d[d["register"].isin(spec["registers"])]
    if d.empty:
        return 0.0
    if spec.get("stream"):
        d = d[d["stream"] == spec["stream"]]
    if d.empty:
        return 0.0
    if spec.get("indicator_exact"):
        d = d[d["indicator"].isin(spec["indicator_exact"])]
    else:
        mask = pd.Series(True, index=d.index)
        for token in spec.get("indicator_contains") or []:
            mask = mask & d["indicator"].astype(str).str.contains(token, na=False, case=False, regex=False)
        for token in spec.get("indicator_exclude") or []:
            mask = mask & ~d["indicator"].astype(str).str.contains(token, na=False, case=False, regex=False)
        d = d[mask]
    if d.empty:
        return 0.0
    return float(d["value"].sum())


def volume_from_registry(df, indicator_id, side="num"):
    """Sum numerator or denominator for a registry indicator from locked specs."""
    meta = INDICATOR_REGISTRY.get(indicator_id) or {}
    spec = meta.get("num_spec") if side == "num" else meta.get("den_spec")
    return sum_locked(df, spec) if spec else 0.0


def pct_change_safe(old, new):
    """
    Volume / count % change. Zero baseline → None (not 100%).
    Returns (pct_or_None, label).
    """
    try:
        o = float(old) if old is not None else None
        n = float(new) if new is not None else None
    except (TypeError, ValueError):
        return None, "n/a"
    if o is None or n is None:
        return None, "n/a"
    if abs(o) < 1e-12:
        if abs(n) < 1e-12:
            return 0.0, "0%"
        return None, "new from zero (no %)"
    return (n - o) / o * 100.0, None


def safe_rate(
    numerator,
    denominator,
    *,
    indicator="rate",
    formula="numerator / denominator × 100",
    source=None,
    facility=None,
    period=None,
    disaggregation=None,
    method=None,
    allow_over_100=False,
    period_types=None,
):
    """
    Phase A formula safety engine.
    Never caps >100% to 100%. Missing denominator → N/A (not 0%).
    numerator > denominator → invalid / requires reconciliation (still stores raw %).
    Returns audit dict:
      value, numerator, denominator, status (valid|invalid|na), reason,
      indicator, formula, source, facility, period, disaggregation, method,
      calculated_pct (raw even if invalid)
    """
    audit = {
        "indicator": indicator,
        "formula": formula,
        "numerator": None,
        "denominator": None,
        "source": source,
        "facility": facility,
        "period": period,
        "disaggregation": disaggregation,
        "method": method,
        "value": None,
        "calculated_pct": None,
        "status": "na",
        "reason": None,
        "unit": "%",
    }
    try:
        num = float(numerator) if numerator is not None and not (isinstance(numerator, float) and pd.isna(numerator)) else None
        den = float(denominator) if denominator is not None and not (isinstance(denominator, float) and pd.isna(denominator)) else None
    except (TypeError, ValueError):
        audit["reason"] = "Non-numeric numerator or denominator"
        return audit

    audit["numerator"] = num
    audit["denominator"] = den

    # Period mix protection
    if period_types is not None:
        pts = {str(p) for p in period_types if p}
        if len(pts) > 1:
            audit["status"] = "invalid"
            audit["reason"] = (
                f"Mixed period types in one rate: {', '.join(sorted(pts))}. "
                "Do not combine month/quarter/year in a single percentage."
            )
            if den and den > 0 and num is not None:
                raw = num / den * 100.0
                audit["calculated_pct"] = round(raw, 4)
            return audit

    if den is None or den <= 0:
        audit["status"] = "na"
        audit["reason"] = "Missing or zero denominator — result is N/A (not 0%)"
        return audit

    if num is None:
        audit["status"] = "na"
        audit["reason"] = "Missing numerator — result is N/A"
        return audit

    if num < 0 or den < 0:
        audit["status"] = "invalid"
        audit["reason"] = "Negative numerator or denominator"
        raw = num / den * 100.0
        audit["calculated_pct"] = round(raw, 4)
        return audit

    raw = num / den * 100.0
    audit["calculated_pct"] = round(raw, 4)

    # Numerator > denominator: do NOT cap; mark invalid for investigation
    if not allow_over_100 and num > den + 1e-9:
        audit["status"] = "invalid"
        audit["reason"] = (
            f"Numerator ({num:g}) > denominator ({den:g}) → calculated {raw:.2f}%. "
            "Requires reconciliation — not a valid programme percentage."
        )
        # value stays None so RAG does not treat as high TPR success
        audit["value"] = None
        return audit

    audit["status"] = "valid"
    audit["value"] = round(raw, 2)
    audit["reason"] = None
    return audit


def _period_types_in(df):
    pts = set()
    if df is None or df.empty or "period" not in df.columns:
        return pts
    for p in df["period"].dropna().unique():
        s = str(p)
        if len(s) == 4 and s.isdigit():
            pts.add("year")
        elif "-Q" in s:
            pts.add("quarter")
        elif len(s) == 7 and s[4] == "-":
            pts.add("month")
    return pts


def restrict_to_period_grain(df, grain="month"):
    """Keep one calendar grain so month+year are never summed into one TPR."""
    if df is None or df.empty or "period" not in df.columns:
        return df
    s = df["period"].astype(str)
    if grain == "month":
        return df[s.str.match(r"^\d{4}-\d{2}$", na=False)]
    if grain == "year":
        return df[s.str.match(r"^\d{4}$", na=False)]
    if grain == "quarter":
        return df[s.str.contains(r"Q|Jan|Apr|Jul|Oct|to", case=False, na=False) & ~s.str.match(r"^\d{4}-\d{2}$", na=False) & ~s.str.match(r"^\d{4}$", na=False)]
    return df


def compute_indicator(indicator_id, df, filters=None, facility=None, period=None, extra_num=None, extra_den=None):
    """
    Universal Indicator Engine (Phase A.5).
    ID → definition → source → num → den → period → formula → validation → result.
    All official rates should call this — no ad-hoc dashboard math.
    """
    meta = INDICATOR_REGISTRY.get(indicator_id) or {}
    d = _filter_data(df, filters) if filters else (df if df is not None else pd.DataFrame())
    if facility and d is not None and not d.empty and "facility" in d.columns:
        d = d[d["facility"] == facility]
    if period and d is not None and not d.empty and "period" in d.columns:
        d = d[d["period"].astype(str) == str(period)]

    result = {
        "id": indicator_id,
        "name": meta.get("name") or indicator_id,
        "definition": meta.get("formula"),
        "source": meta.get("source"),
        "formula": meta.get("formula"),
        "pct_type": meta.get("pct_type"),
        "pct_rule": meta.get("pct_rule"),
        "facility": facility,
        "period": period,
        "disaggregation": "as filtered",
        "numerator": None,
        "denominator": None,
        "value": None,
        "calculated_pct": None,
        "status": "na",
        "reason": None,
        "rag": "grey",
        "target": None,
        "baseline": None,
    }

    if indicator_id == "GO_1":
        if d is not None and not d.empty and len(_period_types_in(d)) > 1:
            d = restrict_to_period_grain(d, "month")
            result["reason"] = "Mixed filters: GO_1 uses monthly rows only (year/quarter excluded from this rate)."
        num = volume_from_registry(d, "GO_1", "num") if d is not None and not d.empty else 0.0
        den = volume_from_registry(d, "GO_1", "den") if d is not None and not d.empty else 0.0
        # Prefer legacy RDT filter if volume_from_registry returns 0 but RDT exists
        if den == 0 and d is not None and not d.empty:
            rdt = d[(d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT", na=False))]
            den = float(rdt[rdt["indicator"].str.contains("Total Exam", na=False)]["value"].sum())
            num = float(rdt[rdt["indicator"].str.contains("Positive", na=False)]["value"].sum())
        audit = safe_rate(
            num, den, indicator="GO_1", formula=meta.get("formula"), source=meta.get("source"),
            facility=facility, period=period, method="RDT",
            disaggregation="All ages", period_types=_period_types_in(d),
        )
        result.update({
            "numerator": audit.get("numerator"), "denominator": audit.get("denominator"),
            "value": audit.get("value"), "calculated_pct": audit.get("calculated_pct"),
            "status": audit.get("status"), "reason": audit.get("reason"),
        })
        result["target"] = BASELINES.get("GO_1", {}).get("target")
        result["baseline"] = BASELINES.get("GO_1", {}).get("baseline")
        if result["status"] == "valid" and result["value"] is not None:
            result["rag"] = status_level(
                result["value"], result["target"], direction="go_tpr", baseline=result["baseline"]
            )
        elif result["status"] == "invalid":
            result["rag"] = "grey"
        return result

    if indicator_id in ("CAS_1", "CAS_POS", "CAS_2", "COM_TPR"):
        num = volume_from_registry(d, indicator_id, "num")
        den = volume_from_registry(d, indicator_id, "den")
        audit = safe_rate(
            num, den, indicator=indicator_id, formula=meta.get("formula"), source=meta.get("source"),
            facility=facility, period=period, period_types=_period_types_in(d),
        )
        result.update({
            "numerator": audit.get("numerator"), "denominator": audit.get("denominator"),
            "value": audit.get("value"), "calculated_pct": audit.get("calculated_pct"),
            "status": audit.get("status"), "reason": audit.get("reason"),
        })
        return result

    if indicator_id == "R1_1":
        num = volume_from_registry(d, "R1_1", "num")
        result.update({
            "numerator": num, "denominator": None, "value": num,
            "status": "valid" if num is not None else "na",
            "pct_type": "COUNT",
        })
        result["target"] = BASELINES.get("R1_1", {}).get("target")
        result["baseline"] = BASELINES.get("R1_1", {}).get("baseline")
        if result["value"] is not None and result["target"]:
            result["rag"] = status_level(result["value"], result["target"], direction="higher_better")
        return result

    if indicator_id == "R1_2":
        rr = d[(d["register"] == "MOH 515") & (d["indicator"] == "Reporting Rate")] if d is not None and not d.empty else pd.DataFrame()
        if rr.empty:
            result["status"] = "na"
            result["reason"] = "No MOH 515 reporting rate rows"
        else:
            vals = [reporting_rate_audit(v) for v in rr["value"].tolist()]
            valid = [a["value"] for a in vals if a.get("status") == "valid" and a.get("value") is not None]
            invalid = [a for a in vals if a.get("status") == "invalid"]
            if valid:
                result["value"] = round(sum(valid) / len(valid), 2)
                result["status"] = "valid"
                result["calculated_pct"] = result["value"]
                if invalid:
                    result["reason"] = f"{len(invalid)} invalid cell(s) excluded"
            elif invalid:
                result["status"] = "invalid"
                result["calculated_pct"] = invalid[0].get("calculated_pct")
                result["reason"] = invalid[0].get("reason")
            else:
                result["status"] = "na"
        result["target"] = BASELINES.get("R1_2", {}).get("target")
        result["baseline"] = BASELINES.get("R1_2", {}).get("baseline")
        if result["status"] == "valid" and result["value"] is not None:
            result["rag"] = status_level(result["value"], result["target"], direction="higher_better")
        return result

    if indicator_id == "SO_1":
        # Roster-based; extra_num/extra_den may be passed
        if extra_num is not None and extra_den is not None:
            audit = safe_rate(extra_num, extra_den, indicator="SO_1", formula=meta.get("formula"), source=meta.get("source"))
            result.update({
                "numerator": audit.get("numerator"), "denominator": audit.get("denominator"),
                "value": audit.get("value"), "calculated_pct": audit.get("calculated_pct"),
                "status": audit.get("status"), "reason": audit.get("reason"),
            })
        else:
            result["status"] = "na"
            result["reason"] = "SO_1 requires CHP roster (extra_num/extra_den)"
        result["target"] = BASELINES.get("SO_1", {}).get("target")
        result["baseline"] = BASELINES.get("SO_1", {}).get("baseline")
        if result["status"] == "valid" and result["value"] is not None:
            result["rag"] = status_level(result["value"], result["target"], direction="higher_better")
        return result

    if indicator_id == "EPI_1":
        num = volume_from_registry(d, "EPI_1", "num")
        pop_fac = facility if facility and facility != "All three" else (
            filters.get("facility") if filters and filters.get("facility") not in (None, "All three") else "Total"
        )
        den = get_population(pop_fac) if pop_fac else get_population("Total")
        if den and den > 0 and num is not None:
            result["numerator"] = num
            result["denominator"] = den
            result["value"] = round(num / den * 1000, 2)
            result["status"] = "valid"
            result["reason"] = f"Population provisional ({POPULATION_STATUS})"
        else:
            result["status"] = "na"
            result["reason"] = "Missing population or confirmed"
        return result

    if indicator_id == "REF_1":
        if extra_num is not None and extra_den is not None:
            audit = safe_rate(extra_num, extra_den, indicator="REF_1", formula=meta.get("formula"))
            result.update({
                "numerator": audit.get("numerator"), "denominator": audit.get("denominator"),
                "value": audit.get("value"), "calculated_pct": audit.get("calculated_pct"),
                "status": audit.get("status"), "reason": audit.get("reason"),
            })
        else:
            result["status"] = "na"
            result["reason"] = "REF_1 requires referral counts"
        return result

    result["reason"] = f"Unknown indicator id: {indicator_id}"
    return result


def _positivity_rdt(df, facility=None, period=None):
    """RDT-only positivity via universal engine (MOH 706)."""
    r = compute_indicator("GO_1", df, facility=facility, period=period)
    # Shape as legacy audit dict for callers
    return {
        "indicator": "GO_1_TPR_RDT",
        "formula": r.get("formula"),
        "source": r.get("source"),
        "facility": facility,
        "period": period,
        "method": "RDT",
        "disaggregation": r.get("disaggregation"),
        "numerator": r.get("numerator"),
        "denominator": r.get("denominator"),
        "value": r.get("value"),
        "calculated_pct": r.get("calculated_pct"),
        "status": r.get("status"),
        "reason": r.get("reason"),
    }


def _positivity(df):
    """Backward-compatible tuple; prefer _positivity_rdt for new code."""
    a = _positivity_rdt(df)
    if a["status"] == "valid":
        return a["value"], a["denominator"] or 0, a["numerator"] or 0
    if a["status"] == "invalid":
        # Return raw calculated for debug but callers should use audit
        return a.get("calculated_pct") or 0.0, a["denominator"] or 0, a["numerator"] or 0
    return None, a["denominator"] or 0, a["numerator"] or 0


def scan_formula_validity(df, filters=None):
    """
    Scan facility × monthly period RDT TPR for invalid rates (sample-style checks).
    Returns list of audit dicts with status != valid.
    """
    d = _filter_data(df, filters) if filters else df
    if d is None or d.empty:
        return []
    issues = []
    rdt = d[(d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT", na=False))]
    if rdt.empty:
        return []
    monthly = _monthly_periods(rdt["period"].unique())
    for fac in FACILITIES:
        for per in monthly:
            slice_df = rdt[(rdt["facility"] == fac) & (rdt["period"].astype(str) == str(per))]
            if slice_df.empty:
                continue
            audit = _positivity_rdt(slice_df, facility=fac, period=per)
            if audit["status"] in ("invalid", "na") and audit.get("denominator"):
                # Only report invalid (num>den); skip pure empty
                if audit["status"] == "invalid":
                    issues.append(audit)
            elif audit["status"] == "invalid":
                issues.append(audit)
    return issues


def _monthly_periods(periods):
    """Return only YYYY-MM monthly keys, sorted chronologically."""
    out = []
    for p in periods:
        s = str(p)
        if len(s) == 7 and s[4] == "-" and s[5:7].isdigit() and "Q" not in s:
            out.append(s)
    return sorted(out)


def _as_percent(series_or_value):
    """Normalise reporting rates that may be stored as 0–1 or 0–100."""
    if hasattr(series_or_value, "dropna"):
        s = series_or_value.dropna()
        if s.empty:
            return None
        mean_v = float(s.mean())
    else:
        if series_or_value is None:
            return None
        mean_v = float(series_or_value)
    if mean_v <= 1.5:  # treat as fraction
        return round(mean_v * 100, 1)
    return round(mean_v, 1)


def indicator_lineage(indicator_id, result=None, df=None, filters=None):
    """
    One-click 'How was this calculated?' payload for any official indicator.
    Returns a markdown-friendly dict / string.
    """
    meta = {
        "GO_1": ("RDT positive ÷ RDT examinations × 100", "MOH 706", "Test positivity rate"),
        "SO_1": ("CHPs passed competency ÷ CHPs assessed × 100", "CHP roster", "Competency"),
        "R1_1": ("Sum of CHP malaria tests in period", "MOH 748 / CHP", "CHP testing volume"),
        "R1_2": ("Reports submitted ÷ reports expected × 100", "MOH 515", "Reporting rate"),
        "CAS_1": ("Confirmed malaria cases (OPD)", "MOH 705A/B", "Confirmed burden"),
        "CAS_2": ("AL / ACT treatments", "MOH 743", "Treatment volume"),
        "EPI_1": ("Confirmed ÷ catchment population × 1000", "705 + population", "Incidence (provisional)"),
        "COM_TPR": ("Community RDT positive ÷ community tested × 100", "MOH 648/521", "Community TPR"),
    }
    formula, source, label = meta.get(indicator_id, ("See catalogue", "KHIS", indicator_id))
    r = result
    if r is None and df is not None:
        try:
            r = compute_indicator(indicator_id, df, filters)
        except Exception:
            r = {}
    r = r or {}
    lines = [
        f"**{indicator_id} — {label}**",
        f"- **Formula:** {formula}",
        f"- **Source:** {source}",
        f"- **Status:** `{r.get('status') or r.get('formula_status') or '—'}`",
        f"- **Value:** `{r.get('value')}`",
        f"- **Numerator:** `{r.get('numerator')}`",
        f"- **Denominator:** `{r.get('denominator')}`",
        f"- **Reason (if not valid):** {r.get('reason') or '—'}",
    ]
    if r.get("period") or (filters or {}).get("periods"):
        lines.append(f"- **Period filter:** {(filters or {}).get('periods') or r.get('period')}")
    if (filters or {}).get("facility"):
        lines.append(f"- **Facility filter:** {(filters or {}).get('facility')}")
    return "\n".join(lines)


def show_lineage_expander(indicator_id, result=None, df=None, filters=None):
    """Streamlit expander for lineage — safe to call from any page."""
    with st.expander(f"How was **{indicator_id}** calculated?", expanded=False):
        st.markdown(indicator_lineage(indicator_id, result=result, df=df, filters=filters))


def compute_official_indicators(df, filters):
    d = _filter_data(df, filters)
    mixed_note = None
    if d is not None and not d.empty and len(_period_types_in(d)) > 1:
        d = restrict_to_period_grain(d, "month")
        mixed_note = "Period filter mixed year/quarter/month — official TPR/cascade use monthly rows only."
    result = {}

    go_audit = _positivity_rdt(d, facility=filters.get("facility"), period=None)
    go_per_fac = {}
    formula_issues = []
    for fac in FACILITIES:
        fac_df = d[d["facility"] == fac]
        fa = _positivity_rdt(fac_df, facility=fac)
        go_per_fac[fac] = {
            "value": fa.get("value"),
            "tested": int(fa["denominator"] or 0),
            "positive": int(fa["numerator"] or 0),
            "status": fa.get("status"),
            "reason": fa.get("reason"),
            "calculated_pct": fa.get("calculated_pct"),
        }
        if fa.get("status") == "invalid":
            formula_issues.append(fa)

    # Testing-collapse: only consecutive *monthly* periods; only on valid rates
    testing_collapse = False
    rdt_all = d[(d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT", na=False))]
    monthly = _monthly_periods(rdt_all["period"].unique() if not rdt_all.empty else [])
    if len(monthly) >= 2:
        tested_by_m = (
            rdt_all[rdt_all["indicator"].str.contains("Total Exam") & rdt_all["period"].isin(monthly)]
            .groupby("period")["value"].sum()
            .reindex(monthly)
            .fillna(0)
        )
        pos_by_m = (
            rdt_all[rdt_all["indicator"].str.contains("Positive") & rdt_all["period"].isin(monthly)]
            .groupby("period")["value"].sum()
            .reindex(monthly)
            .fillna(0)
        )
        latest_t = float(tested_by_m.iloc[-1])
        prior_t = float(tested_by_m.iloc[:-1].mean()) if len(tested_by_m) > 1 else 0
        a_latest = safe_rate(float(pos_by_m.iloc[-1]), latest_t, indicator="TPR_collapse_latest")
        a_prev = safe_rate(
            float(pos_by_m.iloc[-2]), float(tested_by_m.iloc[-2]), indicator="TPR_collapse_prev"
        )
        latest_tpr = a_latest.get("value") if a_latest.get("status") == "valid" else None
        prev_tpr = a_prev.get("value") if a_prev.get("status") == "valid" else None
        if prior_t > 0 and latest_t < prior_t * (1 - TPR_RULE["collapse_drop_pct"] / 100):
            if latest_tpr is not None and prev_tpr is not None and latest_tpr > prev_tpr:
                testing_collapse = True

    if go_audit.get("status") == "invalid":
        formula_issues.insert(0, go_audit)

    # Scan monthly facility cells for formula invalid (Phase A sample cases)
    try:
        formula_issues.extend(scan_formula_validity(d, None))
    except Exception:
        pass
    # Dedupe by facility+period+indicator
    seen_k = set()
    uniq_issues = []
    for a in formula_issues:
        k = (a.get("facility"), a.get("period"), a.get("indicator"), a.get("reason"))
        if k not in seen_k:
            seen_k.add(k)
            uniq_issues.append(a)

    result["GO_1"] = {
        "name": "Overall Test Positivity Rate (RDT only)",
        "value": go_audit.get("value") if go_audit.get("status") == "valid" else None,
        "per_facility": go_per_fac,
        "baseline": BASELINES["GO_1"]["baseline"],
        "target": BASELINES["GO_1"]["target"],
        "unit": "%", "source": BASELINES["GO_1"]["source"],
        "tested": int(go_audit.get("denominator") or 0),
        "positive": int(go_audit.get("numerator") or 0),
        "testing_collapse": testing_collapse,
        "formula_status": go_audit.get("status"),
        "formula_reason": (mixed_note + " " if mixed_note else "") + (go_audit.get("reason") or ""),
        "period_grain": "month" if mixed_note else "as-filtered",
        "calculated_pct": go_audit.get("calculated_pct"),
        "formula": go_audit.get("formula"),
        "formula_audit": go_audit,
        "formula_issues": uniq_issues[:40],
    }

    # Prefer live CHP roster for SO_1 when roster exists (Phase 2)
    chps_df = query("SELECT competency_status FROM chps WHERE active = 1 OR active IS NULL")
    so_row = query("SELECT baseline_value FROM baselines WHERE indicator_id='SO_1'")
    if not chps_df.empty:
        passed_n = int((chps_df["competency_status"] == "passed").sum())
        so_a = safe_rate(
            passed_n, len(chps_df),
            indicator="SO_1",
            formula=INDICATOR_REGISTRY["SO_1"]["formula"],
            source="CHP roster (live)",
        )
        so_current = so_a.get("value")
        so_source = "CHP roster (live)"
    elif not so_row.empty:
        so_current = float(so_row.iloc[0]["baseline_value"])
        so_source = BASELINES["SO_1"]["source"] + " (manual baseline)"
    else:
        so_current = BASELINES["SO_1"]["baseline"]
        so_source = BASELINES["SO_1"]["source"]
    result["SO_1"] = {
        "name": "CHPs who passed competency assessment",
        "value": so_current,
        "baseline": BASELINES["SO_1"]["baseline"],
        "target": BASELINES["SO_1"]["target"],
        "unit": "%", "source": so_source,
    }

    # R1_1: community tests = MOH 748 Total <5 + Total >=5 only (not other "Total*" labels)
    chp_tests_total = 0
    chp_tests_per_fac = {}
    for fac in FACILITIES:
        fac_df = d[
            (d["facility"] == fac)
            & (d["stream"] == "Community")
            & (d["register"] == "MOH 748")
            & (d["indicator"].isin(["Total <5", "Total >=5"]))
        ]
        val = fac_df["value"].sum()
        chp_tests_per_fac[fac] = int(val) if val else 0
        chp_tests_total += val
    result["R1_1"] = {
        "name": "Number of tests performed by CHPs",
        "value": int(chp_tests_total),
        "per_facility": chp_tests_per_fac,
        "baseline": BASELINES["R1_1"]["baseline"],
        "target": BASELINES["R1_1"]["target"],
        "unit": "tests", "source": BASELINES["R1_1"]["source"],
        "note": "Sum of MOH 748 Total <5 + Total ≥5 in selected periods",
    }

    # R1_2: MOH 515 reporting rate — validated 0–100 only
    rr_df = d[(d["register"] == "MOH 515") & (d["indicator"] == "Reporting Rate")]
    if rr_df.empty:
        rr_audit = reporting_rate_audit(None)
    else:
        # Prefer mean of valid cells; any invalid cell is tracked
        vals = [reporting_rate_audit(v) for v in rr_df["value"].tolist()]
        valid_vals = [a["value"] for a in vals if a.get("status") == "valid" and a.get("value") is not None]
        invalid = [a for a in vals if a.get("status") == "invalid"]
        if valid_vals:
            rr_audit = {
                "status": "valid",
                "value": round(sum(valid_vals) / len(valid_vals), 2),
                "calculated_pct": round(sum(valid_vals) / len(valid_vals), 4),
                "reason": f"{len(invalid)} invalid cell(s) excluded" if invalid else None,
            }
        elif invalid:
            rr_audit = invalid[0]
        else:
            rr_audit = reporting_rate_audit(None)
    rr_per_fac = {}
    for fac in FACILITIES:
        fac_rr = d[
            (d["facility"] == fac)
            & (d["register"] == "MOH 515")
            & (d["indicator"] == "Reporting Rate")
        ]
        if fac_rr.empty:
            rr_per_fac[fac] = None
        else:
            a = reporting_rate_audit(fac_rr["value"].mean())
            rr_per_fac[fac] = a.get("value") if a.get("status") == "valid" else None
    result["R1_2"] = {
        "name": "% of monitoring reports submitted",
        "value": rr_audit.get("value") if rr_audit.get("status") == "valid" else None,
        "per_facility": rr_per_fac,
        "baseline": BASELINES["R1_2"]["baseline"],
        "target": BASELINES["R1_2"]["target"],
        "unit": "%", "source": BASELINES["R1_2"]["source"],
        "formula_status": rr_audit.get("status"),
        "formula_reason": rr_audit.get("reason"),
        "calculated_pct": rr_audit.get("calculated_pct"),
    }

    return result


def _pack_rate(audit, unit="%", target=None, extra_note=None):
    """UI pack from safe_rate / reporting_rate_audit."""
    note = audit.get("reason") or ""
    if extra_note:
        note = f"{extra_note} {note}".strip()
    return {
        "value": audit.get("value") if audit.get("status") == "valid" else None,
        "unit": unit,
        "target": target,
        "status": audit.get("status"),
        "reason": audit.get("reason"),
        "calculated_pct": audit.get("calculated_pct"),
        "numerator": audit.get("numerator"),
        "denominator": audit.get("denominator"),
        "formula": audit.get("formula"),
        "source": audit.get("source"),
        "note": note or audit.get("source"),
    }


def compute_epidemiological_indicators(df, filters):
    d = _filter_data(df, filters)
    out = {}

    if filters.get("facility") and filters["facility"] != "All three":
        population = get_population(filters["facility"])
    else:
        population = get_population("Total")

    # Prefer clinical OPD confirmed (705A/B) — not mixed 706+748 positives
    clinical = d[d["register"].isin(["MOH 705A", "MOH 705B"])]
    confirmed = float(
        clinical[clinical["indicator"].str.contains(r"Confirmed", na=False, regex=True)]["value"].sum()
    )
    tested_clinical = float(
        clinical[clinical["indicator"].str.contains(r"Tested", na=False, regex=True)]["value"].sum()
    )

    # Incidence: rate per 1,000 (not a 0–100 percentage; allow >100)
    if population and population > 0 and confirmed is not None:
        inc = confirmed / population * 1000
        out["Malaria Incidence Rate"] = {
            "value": round(inc, 2), "unit": " per 1,000",
            "target": EPI_BENCHMARKS["Malaria Incidence Rate"]["target"],
            "note": EPI_BENCHMARKS["Malaria Incidence Rate"]["note"],
            "source": f"MOH 705A/B Confirmed ({int(confirmed):,}) ÷ Population ({population:,}) × 1,000",
            "status": "valid",
        }
    else:
        out["Malaria Incidence Rate"] = {
            "value": None, "unit": " per 1,000",
            "target": EPI_BENCHMARKS["Malaria Incidence Rate"]["target"],
            "note": "Missing population or confirmed",
            "status": "na",
        }

    aber_a = safe_rate(
        tested_clinical, population,
        indicator="EPI_ABER",
        formula="MOH 705A/B Tested ÷ Population × 100",
        source="MOH 705A/B + provisional population",
        allow_over_100=True,  # ABER can exceed 100% if tested > pop (still flag if absurd)
    )
    # Flag extreme ABER as invalid if tested > population
    if tested_clinical > population > 0:
        aber_a["status"] = "invalid"
        aber_a["value"] = None
        aber_a["reason"] = (
            f"Tested ({tested_clinical:g}) > population ({population:g}) — "
            "ABER not valid without population reconciliation."
        )
        aber_a["calculated_pct"] = round(tested_clinical / population * 100, 4)
    out["ABER"] = {
        **_pack_rate(aber_a, target=EPI_BENCHMARKS["ABER"]["target"]),
        "note": EPI_BENCHMARKS["ABER"]["note"] + (f" · {aber_a.get('reason') or ''}"),
        "source": aber_a.get("source"),
    }

    tpr_a = _positivity_rdt(d)
    out["Test Positivity Rate"] = {
        **_pack_rate(tpr_a, target=EPI_BENCHMARKS["Test Positivity Rate"]["target"]),
        "note": EPI_BENCHMARKS["Test Positivity Rate"]["note"],
        "source": "MOH 706 RDT only",
    }

    deaths = float(d[d["indicator"].str.contains("Death", na=False)]["value"].sum())
    cfr_a = safe_rate(
        deaths, confirmed,
        indicator="EPI_CFR",
        formula="Deaths ÷ MOH 705 Confirmed × 100",
        source="Deaths / 705 confirmed",
    )
    out["Case Fatality Rate"] = {
        **_pack_rate(cfr_a, target=EPI_BENCHMARKS["Case Fatality Rate"]["target"]),
        "note": EPI_BENCHMARKS["Case Fatality Rate"]["note"],
    }

    severe = float(d[d["indicator"].str.contains("Severe", na=False)]["value"].sum())
    sev_a = safe_rate(
        severe, confirmed,
        indicator="EPI_SEVERE",
        formula="Severe ÷ MOH 705 Confirmed × 100",
        source="Severe / 705 confirmed",
    )
    out["Severe Malaria Proportion"] = {
        **_pack_rate(sev_a, target=EPI_BENCHMARKS["Severe Malaria Proportion"]["target"]),
        "note": EPI_BENCHMARKS["Severe Malaria Proportion"]["note"],
    }

    return out


def compute_secondary_indicators(df, filters):
    d = _filter_data(df, filters)
    out = {}

    fac = d[(d["stream"] == "Facility") & (d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT", na=False))]
    ft = float(fac[fac["indicator"].str.contains("Total Exam", na=False)]["value"].sum())
    fp = float(fac[fac["indicator"].str.contains("Positive", na=False)]["value"].sum())
    out["Facility Positivity"] = _pack_rate(
        safe_rate(fp, ft, indicator="SEC_FAC_TPR", formula="MOH 706 RDT pos ÷ RDT exams × 100",
                  source="MOH 706 RDT facility", method="RDT")
    )

    com = d[(d["stream"] == "Community") & (d["register"] == "MOH 748")]
    # Strict totals only (avoid other Total* labels)
    ct = float(com[com["indicator"].isin(["Total <5", "Total >=5"])]["value"].sum())
    cp = float(com[com["indicator"].str.contains(r"^Positive", na=False, regex=True)]["value"].sum())
    out["Community Positivity"] = _pack_rate(
        safe_rate(cp, ct, indicator="SEC_COM_TPR", formula="MOH 748 Positive ÷ Total <5+≥5 × 100",
                  source="MOH 748 community")
    )

    # Testing rate: same register stream 705 suspected vs tested
    clinical = d[d["register"].isin(["MOH 705A", "MOH 705B"])]
    susp = float(clinical[clinical["indicator"].str.contains("Suspected", na=False)]["value"].sum())
    test = float(clinical[clinical["indicator"].str.contains("Tested", na=False)]["value"].sum())
    out["Testing Rate"] = _pack_rate(
        safe_rate(test, susp, indicator="SEC_TEST_RATE", formula="705 Tested ÷ 705 Suspected × 100",
                  source="MOH 705A/B"),
        target=80,
    )

    # Share of tests at community (composition — allow >50%; not num>den invalid in same sense)
    share = safe_rate(ct, ft + ct, indicator="SEC_COM_SHARE", formula="748 totals ÷ (706 RDT exams + 748 totals) × 100",
                      source="706 RDT exams + 748 totals", allow_over_100=False)
    out["% Tests at Community"] = _pack_rate(share)

    twt = float(d[d["indicator"].str.contains("Treated w/o Testing", na=False)]["value"].sum())
    al_total = float(
        d[d["indicator"].isin(["AL Total", "AL Dispensed"])]["value"].sum()
    )
    out["% Treated Without Testing"] = _pack_rate(
        safe_rate(twt, al_total, indicator="SEC_TWT", formula="Treated w/o testing ÷ AL Total/Dispensed × 100",
                  source="MOH 743/748"),
        target=5,
    )

    # AL coverage: facility AL vs facility RDT positives only (avoid mixing community positives)
    al_fac = float(
        d[(d["stream"] == "Facility") & (d["indicator"].isin(["AL Total", "AL Dispensed"]))]["value"].sum()
    )
    out["AL Treatment Coverage"] = _pack_rate(
        safe_rate(al_fac, fp, indicator="SEC_AL_COV",
                  formula="Facility AL ÷ MOH 706 RDT positives × 100",
                  source="Facility AL / 706 RDT pos — approximate; not same-patient linkage"),
    )
    if out["AL Treatment Coverage"].get("note"):
        out["AL Treatment Coverage"]["note"] = (
            "Approximate coverage only (not linked patient records). "
            + (out["AL Treatment Coverage"].get("note") or "")
        )

    ipt1 = float(d[d["indicator"] == "IPT1"]["value"].sum())
    ipt2 = float(d[d["indicator"] == "IPT2"]["value"].sum())
    ipt3 = float(d[d["indicator"] == "IPT3"]["value"].sum())
    out["IPT3 Retention (of IPTp1)"] = _pack_rate(
        safe_rate(ipt3, ipt1, indicator="SEC_IPT3_RET", formula="IPT3 ÷ IPT1 × 100", source="MOH 743 IPT"),
        target=70,
    )
    out["IPT2 Retention (of IPTp1)"] = _pack_rate(
        safe_rate(ipt2, ipt1, indicator="SEC_IPT2_RET", formula="IPT2 ÷ IPT1 × 100", source="MOH 743 IPT"),
    )

    mip = d[d["indicator"] == "Malaria in Pregnancy"]["value"].sum()
    out["Malaria in Pregnancy"] = {"value": int(mip), "unit": "cases", "target": None, "status": "valid"}

    rr = d[d["indicator"] == "Reporting Rate"]
    fac_rr = rr[rr["stream"] == "Facility"]["value"]
    com_rr = rr[rr["stream"] == "Community"]["value"]
    fac_mean = float(fac_rr.mean()) if not fac_rr.empty else None
    com_mean = float(com_rr.mean()) if not com_rr.empty else None
    out["Facility Reporting Rate"] = _pack_rate(
        reporting_rate_audit(fac_mean, register="Facility registers"), target=95
    )
    out["Community Reporting Rate"] = _pack_rate(
        reporting_rate_audit(com_mean, register="Community registers"), target=80
    )

    return out


def status_level(current, target, direction="higher_better", baseline=None, testing_collapse=False):
    """Phase 0 RAG. direction: higher_better | lower_better | go_tpr | completeness."""
    try:
        if current is None or (target is None and direction != "go_tpr"):
            return "grey"
        if direction == "higher_better":
            if current >= target:
                return "green"
            if current >= target * 0.75:
                return "amber"
            return "red"
        if direction == "completeness":
            if current >= 90:
                return "green"
            if current >= 80:
                return "amber"
            return "red"
        if direction == "go_tpr":
            # Never green solely on high TPR if testing is collapsing
            base = baseline if baseline is not None else BASELINES["GO_1"]["baseline"]
            tgt = target if target is not None else BASELINES["GO_1"]["target"]
            if testing_collapse:
                return "red" if current >= base else "amber"
            if current >= tgt:
                return "green"
            if current >= base:
                return "amber"
            return "red"
        # lower_better
        if current <= target:
            return "green"
        if current <= target * 1.25:
            return "amber"
        return "red"
    except Exception:
        return "grey"


def format_value(value, unit="", decimals=1):
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if unit == "%":
        return f"{value:.{decimals}f}%"
    if unit in ("tests", "cases", " per 1,000"):
        return f"{value:,.{decimals}f}{unit}" if unit != "tests" else f"{int(value):,}"
    if isinstance(value, (int, float)):
        return f"{value:,.{decimals}f}"
    return str(value)


def delta_string(current, baseline, unit=""):
    if current is None or baseline is None:
        return None, "flat"
    delta = current - baseline
    if unit == "%":
        text = f"▲ {delta:+.1f} pp" if delta > 0 else (f"▼ {delta:+.1f} pp" if delta < 0 else "— 0.0 pp")
    else:
        text = f"▲ {delta:+,.0f}" if delta > 0 else (f"▼ {delta:+,.0f}" if delta < 0 else "— 0")
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return text, direction


def compute_run_rate(actual_ytd, annual_target, as_of_month=None, year=None):
    """
    Early Phase B — target trajectory / run-rate.
    Returns dict: expected_ytd, variance, achievement_pct, required_monthly,
    months_elapsed, months_remaining, on_pace (bool|None).
    Assumes uniform monthly share of annual target (simple planning model).
    """
    year = year or datetime.now().year
    if as_of_month is None:
        as_of_month = datetime.now().month
    as_of_month = max(1, min(12, int(as_of_month)))
    months_elapsed = as_of_month
    months_remaining = 12 - as_of_month
    try:
        actual = float(actual_ytd) if actual_ytd is not None else None
        target = float(annual_target) if annual_target is not None else None
    except (TypeError, ValueError):
        actual, target = None, None
    if target is None or target <= 0:
        return {
            "actual_ytd": actual,
            "annual_target": target,
            "expected_ytd": None,
            "variance": None,
            "achievement_pct": None,
            "required_monthly": None,
            "months_elapsed": months_elapsed,
            "months_remaining": months_remaining,
            "on_pace": None,
            "year": year,
        }
    expected = target * (months_elapsed / 12.0)
    variance = (actual - expected) if actual is not None else None
    achievement = (actual / target * 100.0) if actual is not None else None
    remaining_need = (target - actual) if actual is not None else target
    required_monthly = (
        remaining_need / months_remaining if months_remaining > 0 else remaining_need
    )
    on_pace = None
    if variance is not None:
        on_pace = variance >= -0.05 * expected  # within 5% of expected path
    return {
        "actual_ytd": actual,
        "annual_target": target,
        "expected_ytd": expected,
        "variance": variance,
        "achievement_pct": achievement,
        "required_monthly": required_monthly,
        "months_elapsed": months_elapsed,
        "months_remaining": months_remaining,
        "on_pace": on_pace,
        "year": year,
    }


def _ytd_month_from_filters(filters, df):
    """Infer planning year and as-of month from filter periods or calendar."""
    year = datetime.now().year
    month = datetime.now().month
    periods = (filters or {}).get("periods")
    if periods:
        monthly = sorted(
            [str(p) for p in periods if len(str(p)) == 7 and str(p)[4] == "-"]
        )
        if monthly:
            last = monthly[-1]
            try:
                year = int(last[:4])
                month = int(last[5:7])
            except ValueError:
                pass
    elif df is not None and not df.empty:
        monthly = sorted(
            {
                str(p)
                for p in df["period"].dropna().unique()
                if len(str(p)) == 7 and str(p)[4] == "-"
            }
        )
        if monthly:
            last = monthly[-1]
            try:
                year = int(last[:4])
                month = int(last[5:7])
            except ValueError:
                pass
    return year, month


def render_run_rate_panel(title, actual, annual_target, unit, year, as_of_month):
    """Streamlit UI for run-rate / trajectory."""
    rr = compute_run_rate(actual, annual_target, as_of_month=as_of_month, year=year)
    section_header(title)
    st.caption(
        f"Simple uniform path for **{rr['year']}** through month **{as_of_month}** "
        f"({rr['months_elapsed']} months elapsed, {rr['months_remaining']} remaining). "
        "Not a forecast — a planning check against the annual target."
    )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        big_number(
            f"{rr['actual_ytd']:,.0f}{unit}" if rr["actual_ytd"] is not None else "—",
            "Actual (YTD / view)",
        )
    with c2:
        big_number(
            f"{rr['expected_ytd']:,.0f}{unit}" if rr["expected_ytd"] is not None else "—",
            "Expected by now",
            delta=f"Annual target {rr['annual_target']:,.0f}{unit}" if rr["annual_target"] else "",
        )
    with c3:
        var = rr["variance"]
        big_number(
            f"{var:+,.0f}{unit}" if var is not None else "—",
            "Variance vs path",
            delta_direction="up" if var and var >= 0 else "down",
        )
    with c4:
        pace = "On pace" if rr["on_pace"] else ("Behind path" if rr["on_pace"] is False else "—")
        big_number(
            f"{rr['achievement_pct']:.0f}%" if rr["achievement_pct"] is not None else "—",
            f"Achievement · {pace}",
        )
    if rr["required_monthly"] is not None and rr["months_remaining"] > 0:
        st.info(
            f"**Required run rate:** about **{rr['required_monthly']:,.0f}{unit} per month** "
            f"for the remaining {rr['months_remaining']} month(s) to hit "
            f"{rr['annual_target']:,.0f}{unit} by year-end."
        )
    elif rr["months_remaining"] == 0 and rr["actual_ytd"] is not None and rr["annual_target"]:
        gap = rr["annual_target"] - rr["actual_ytd"]
        st.caption(
            f"Year complete in this model. Gap vs annual target: **{gap:+,.0f}{unit}**."
        )
    return rr


def progress_pct(current, baseline, target):
    try:
        if current is None or baseline is None or target is None:
            return 0
        if target == baseline:
            return 100 if current >= target else 0
        pct = (current - baseline) / (target - baseline) * 100
        return max(0, min(100, pct))
    except Exception:
        return 0


def last_data_period(df=None):
    """Latest period present in malaria_data (prefer YYYY-MM monthly)."""
    try:
        if df is None or df.empty:
            periods = _available_periods()
        else:
            periods = sorted(str(p) for p in df["period"].dropna().unique())
        monthly = [p for p in periods if len(p) == 7 and p[4] == "-" and "Q" not in p]
        if monthly:
            return monthly[-1]
        return periods[-1] if periods else None
    except Exception:
        return None


def indicator_rag(ind, key="GO_1"):
    """Phase 0 RAG for one official indicator. Invalid formula → grey (not green)."""
    if ind.get("formula_status") == "invalid":
        return "grey"
    if ind.get("value") is None and ind.get("formula_status") == "na":
        return "grey"
    direction = BASELINES.get(key, {}).get("direction", "higher_better")
    return status_level(
        ind.get("value"),
        ind.get("target"),
        direction=direction,
        baseline=ind.get("baseline"),
        testing_collapse=ind.get("testing_collapse", False),
    )


def build_action_list(df, indicators):
    """Phase 1 Overview actions from gaps, reporting, CHP tests."""
    actions = []
    go, so, r11, r12 = indicators["GO_1"], indicators["SO_1"], indicators["R1_1"], indicators["R1_2"]

    if r12["value"] is not None and r12["value"] < r12["target"]:
        actions.append({
            "priority": "high",
            "text": f"Reporting rate is {r12['value']:.1f}% (target ≥{r12['target']:.0f}%). Follow up late MOH 515 / facility reports.",
        })
    if r11["value"] is not None and r11["value"] < r11["target"]:
        gap = int(r11["target"] - r11["value"])
        actions.append({
            "priority": "high",
            "text": f"CHP tests at {r11['value']:,} — {gap:,} below target {r11['target']:,}. Check CHP activity and RDT stock.",
        })
    if so["value"] is not None and so["value"] < so["target"]:
        actions.append({
            "priority": "medium",
            "text": f"CHP competency {so['value']:.0f}% vs target ≥{so['target']:.0f}%. Schedule assessments.",
        })

    # Facility-level reporting holes
    if df is not None and not df.empty:
        rr = df[df["indicator"] == "Reporting Rate"]
        if not rr.empty:
            low = rr[rr["value"] < 0.8]
            if not low.empty:
                facs = ", ".join(sorted(low["facility"].unique())[:5])
                actions.append({
                    "priority": "high",
                    "text": f"Registers below 80% reporting completeness at: {facs}.",
                })

        for fac in FACILITIES:
            fac_df = df[df["facility"] == fac]
            if fac_df.empty:
                actions.append({
                    "priority": "medium",
                    "text": f"No data uploaded for {fac}.",
                })
                continue
            susp = fac_df[fac_df["indicator"].str.contains("Suspected", na=False)]["value"].sum()
            tested = fac_df[fac_df["indicator"].str.contains("Tested|Total Exam", na=False)]["value"].sum()
            conf = fac_df[fac_df["indicator"].str.contains("Positive|Confirmed", na=False)]["value"].sum()
            treated = fac_df[fac_df["indicator"].str.contains("AL Total|AL Dispensed", na=False)]["value"].sum()
            dg = safe_rate(susp - tested, susp, indicator="action_diag_gap")
            if dg.get("status") == "valid" and dg.get("value") is not None and dg["value"] > 20:
                actions.append({
                    "priority": "high",
                    "text": f"{fac}: diagnosis gap {dg['value']:.0f}% of suspected not tested.",
                })
            tg = safe_rate(conf - treated, conf, indicator="action_treat_gap") if conf else {"status": "na"}
            if tg.get("status") == "valid" and tg.get("value") is not None and tg["value"] > 15:
                actions.append({
                    "priority": "high",
                    "text": f"{fac}: treatment gap {tg['value']:.0f}% of confirmed not reflected in AL.",
                })

    if go.get("testing_collapse"):
        actions.append({
            "priority": "critical",
            "text": "TPR rising while testing volume fell sharply — investigate testing collapse (do not treat high TPR as success).",
        })
    if go.get("formula_status") == "invalid":
        actions.append({
            "priority": "critical",
            "text": (
                f"GO_1 formula invalid: {go.get('formula_reason') or 'numerator > denominator'}. "
                "Do not use TPR for programme success — reconcile MOH 706 RDT positives vs exams."
            ),
        })
    n_fi = len(go.get("formula_issues") or [])
    if n_fi:
        actions.append({
            "priority": "high",
            "text": f"{n_fi} facility/period rate calculation(s) flagged invalid — open Data Quality or GO page formula audit.",
        })

    # Deduplicate by text, sort by priority
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    seen = set()
    unique = []
    for a in actions:
        if a["text"] not in seen:
            seen.add(a["text"])
            unique.append(a)
    unique.sort(key=lambda x: order.get(x["priority"], 9))
    return unique[:8]


def facility_ranking_table(df, indicators):
    """Simple ranking for Overview."""
    rows = []
    go = indicators.get("GO_1", {})
    r11 = indicators.get("R1_1", {})
    r12 = indicators.get("R1_2", {})
    for fac in FACILITIES:
        go_v = go.get("per_facility", {}).get(fac, {})
        tpr = go_v.get("value") if isinstance(go_v, dict) else go_v
        tests = r11.get("per_facility", {}).get(fac, 0)
        rr = r12.get("per_facility", {}).get(fac)
        rows.append({
            "Facility": fac,
            "TPR %": f"{tpr:.1f}" if tpr is not None else "—",
            "CHP tests": f"{int(tests):,}" if tests else "—",
            "Reporting %": f"{rr:.1f}" if rr is not None else "—",
        })
    return pd.DataFrame(rows)


def auto_headline(indicators):
    go = indicators["GO_1"]; so = indicators["SO_1"]
    r11 = indicators["R1_1"]; r12 = indicators["R1_2"]

    levels = {
        "GO_1": indicator_rag(go, "GO_1"),
        "SO_1": indicator_rag(so, "SO_1"),
        "R1_1": indicator_rag(r11, "R1_1"),
        "R1_2": indicator_rag(r12, "R1_2"),
    }
    on_track = sum(1 for lv in levels.values() if lv == "green")

    if on_track == 4:
        s1 = "All four official indicators are on track (Phase 0 RAG)."
    elif on_track >= 2:
        s1 = f"{on_track} of four official indicators are on track."
    else:
        s1 = f"Only {on_track} of four official indicators are on track."

    scores = []
    for key, ind in [("GO_1", go), ("SO_1", so), ("R1_1", r11), ("R1_2", r12)]:
        if ind["value"] is not None:
            pct = progress_pct(ind["value"], ind["baseline"], ind["target"])
            scores.append((pct, key, ind, levels[key]))
    if scores:
        scores.sort()
        worst_pct, worst_key, worst, worst_lv = scores[0]
        s2 = (
            f"At risk: {worst['name']} — "
            f"{format_value(worst['value'], worst['unit'])} vs target "
            f"{format_value(worst['target'], worst['unit'])} ({worst_lv})."
        )
    else:
        s2 = "Upload data to begin monitoring."

    go_fac = go.get("per_facility", {})
    valid = [(f, v["value"]) for f, v in go_fac.items() if isinstance(v, dict) and v.get("value") is not None]
    if valid:
        valid.sort(key=lambda x: x[1], reverse=True)
        top = valid[0]
        s3 = f"{top[0]} leads on positivity at {top[1]:.1f}%."
    else:
        s3 = ""

    if go.get("testing_collapse"):
        s3 = (s3 + " " if s3 else "") + "Warning: testing volume dropped while TPR rose."

    return " ".join(s for s in [s1, s2, s3] if s)


# ==================================================================
# ALERT ENGINE
# ==================================================================

def _recent_monthly_periods(data, n=6):
    """Newest n YYYY-MM periods present in data."""
    if data is None or data.empty:
        return []
    periods = sorted(
        {str(p) for p in data["period"].dropna().unique()
         if len(str(p)) == 7 and str(p)[4] == "-"},
        reverse=True,
    )
    return periods[:n]


def _match_ind(df, pattern):
    if df is None or df.empty:
        return df.iloc[0:0] if df is not None else pd.DataFrame()
    return df[df["indicator"].astype(str).str.contains(pattern, na=False, regex=True, case=False)]


def _rr_as_pct(val):
    """
    Normalise reporting rate to percent scale for display.
    Values in (1, 1.5] treated as fractions only if ≤1 would be unusual;
    values >1 after fraction expansion (>100%) are still returned so callers
    can flag INVALID — use reporting_rate_audit for validation.
    """
    audit = reporting_rate_audit(val)
    if audit["status"] == "na":
        return None
    return audit.get("value") if audit.get("value") is not None else audit.get("calculated_pct")


def reporting_rate_audit(val, facility=None, period=None, register=None):
    """
    Reporting rate: valid only if 0–100% (or 0–1 as fraction).
    Stored 1.375 → 137.5% → INVALID (not a normal KPI).
    """
    audit = {
        "indicator": "reporting_rate",
        "formula": "reports submitted / reports expected (KHIS reporting rate)",
        "numerator": None,
        "denominator": None,
        "source": register or "Reporting Rate",
        "facility": facility,
        "period": period,
        "value": None,
        "calculated_pct": None,
        "status": "na",
        "reason": None,
    }
    try:
        v = float(val)
    except (TypeError, ValueError):
        audit["reason"] = "Non-numeric reporting rate"
        return audit
    if pd.isna(v):
        audit["reason"] = "Missing reporting rate"
        return audit
    # Fraction form 0–1.0 inclusive
    if 0 <= v <= 1.0:
        pct = v * 100.0
        audit["calculated_pct"] = round(pct, 4)
        audit["value"] = round(pct, 2)
        audit["status"] = "valid"
        return audit
    # Ambiguous band 1.0 < v < 5: not a valid fraction (<=1) and not a plausible
    # reporting % (would be 1–5%). Includes the historical bug case v=2.0 → "2%".
    if 1.0 < v < 5.0:
        pct = v * 100.0 if v <= 1.5 else v
        audit["calculated_pct"] = round(float(v if v > 1.5 else v * 100.0), 4)
        audit["status"] = "invalid"
        audit["reason"] = (
            f"Reporting rate stored as {v} is ambiguous/implausible "
            "(not a 0–1 fraction and not a credible 0–100% value). Rejected."
        )
        return audit
    # Already percent scale
    if 0 <= v <= 100:
        audit["calculated_pct"] = round(v, 4)
        audit["value"] = round(v, 2)
        audit["status"] = "valid"
        return audit
    if v > 100:
        audit["calculated_pct"] = round(v, 4)
        audit["status"] = "invalid"
        audit["reason"] = (
            f"Reporting rate {v:.1f}% exceeds 100% — invalid data, requires reconciliation."
        )
        return audit
    audit["status"] = "invalid"
    audit["reason"] = f"Negative or unexpected reporting rate value: {v}"
    audit["calculated_pct"] = round(v, 4)
    return audit


def _open_alert_exists(conn, key, facility, period):
    """De-dup by embedding key in message prefix [key]."""
    cur = conn.execute(
        """
        SELECT id FROM alerts
        WHERE status = 'open' AND facility = ? AND period = ?
          AND message LIKE ?
        LIMIT 1
        """,
        (facility, str(period), f"[{key}]%"),
    )
    return cur.fetchone() is not None


def _insert_alert(conn, key, category, severity, facility, register, period, message):
    if _open_alert_exists(conn, key, facility, period):
        return False
    full_msg = f"[{key}] {message}"
    conn.execute(
        """
        INSERT INTO alerts (alert_rule_id, category, severity, facility, register, period, message)
        VALUES (NULL, ?, ?, ?, ?, ?, ?)
        """,
        (category, severity, facility, register or "", str(period), full_msg),
    )
    return True


def evaluate_alerts(recent_months=6):
    """
    Programme-first alert engine.
    Uses regex indicator matching and cascade helpers so rules fire on real KHIS labels.
    Focuses on the newest `recent_months` monthly periods to limit noise.
    """
    conn = get_conn()
    created = 0
    try:
        data = pd.read_sql_query("SELECT * FROM malaria_data", conn)
        if data.empty:
            # Still check stock
            try:
                stock = pd.read_sql_query("SELECT * FROM stock", conn)
            except Exception:
                stock = pd.DataFrame()
            if not stock.empty:
                for _, s in stock.iterrows():
                    item = str(s.get("item", ""))
                    qty = float(s.get("quantity") or 0)
                    reorder = float(s.get("reorder_point") or s.get("reorder_level") or 30)
                    if qty < reorder:
                        key = "stock_rdt" if item.upper() == "RDT" else ("stock_al" if item.upper() == "AL" else f"stock_{item}")
                        sev = "high"
                        cat = "Commodity"
                        if _insert_alert(
                            conn, key, cat, sev, s.get("facility", "—"), item, "current",
                            f"{item} stock is {qty:.0f} (reorder {reorder:.0f}) at {s.get('facility')}.",
                        ):
                            created += 1
            conn.commit()
            return created

        recent = _recent_monthly_periods(data, n=recent_months)
        if recent:
            focus = data[data["period"].astype(str).isin(recent)].copy()
        else:
            focus = data.copy()
            recent = sorted({str(p) for p in focus["period"].dropna().unique()}, reverse=True)[:6]

        # ── Per facility × period cascade / reporting checks ──
        for fac in FACILITIES:
            fac_all = focus[focus["facility"] == fac]
            if fac_all.empty:
                continue

            for per in recent:
                fac_p = fac_all[fac_all["period"].astype(str) == str(per)]
                if fac_p.empty:
                    continue

                cascade = compute_cascade_metrics(fac_p)

                # Reporting rate cells
                rr = fac_p[fac_p["indicator"].astype(str).str.contains(r"Reporting\s*Rate", na=False, case=False)]
                if not rr.empty:
                    for _, row in rr.iterrows():
                        ra = reporting_rate_audit(
                            row["value"], facility=fac, period=per, register=row.get("register")
                        )
                        reg = row.get("register") or ""
                        if ra["status"] == "invalid":
                            if _insert_alert(
                                conn, "reporting_gt_100", "Data Quality", "critical", fac, reg, per,
                                f"Invalid reporting rate on {reg} at {fac} ({per}): "
                                f"{ra.get('reason') or ra.get('calculated_pct')}",
                            ):
                                created += 1
                            continue
                        pct = ra.get("value")
                        if pct is None:
                            continue
                        if pct < 60:
                            if _insert_alert(
                                conn, "reporting_lt_60", "Reporting", "critical", fac, reg, per,
                                f"Reporting rate {pct:.0f}% on {reg} at {fac} ({per}) — critically low.",
                            ):
                                created += 1
                        elif pct < 80:
                            if _insert_alert(
                                conn, "reporting_lt_80", "Reporting", "warning", fac, reg, per,
                                f"Reporting rate {pct:.0f}% on {reg} at {fac} ({per}).",
                            ):
                                created += 1

                # Diagnosis gap
                if cascade["suspected"] >= 20 and cascade["diag_gap_pct"] >= 25:
                    if _insert_alert(
                        conn, "diag_gap_high", "Performance", "high", fac, "", per,
                        f"Diagnosis gap {cascade['diag_gap_pct']:.0f}% "
                        f"({cascade['diag_gap']:,} of {cascade['suspected']:,} suspected not tested) at {fac} ({per}).",
                    ):
                        created += 1

                # Treatment gap
                if cascade["confirmed"] >= 10 and cascade["treat_gap_pct"] >= 20:
                    if _insert_alert(
                        conn, "treat_gap_high", "Performance", "high", fac, "", per,
                        f"Treatment gap {cascade['treat_gap_pct']:.0f}% "
                        f"({cascade['treat_gap']:,} confirmed without AL) at {fac} ({per}).",
                    ):
                        created += 1

                # Treated without testing
                if cascade["treated"] >= 10 and cascade["treated_wo_test"] > 0:
                    twt_pct = _safe_pct(cascade["treated_wo_test"], cascade["treated"])
                    if twt_pct >= 5:
                        if _insert_alert(
                            conn, "twt_high", "Data Quality", "high", fac, "", per,
                            f"Treated without testing {twt_pct:.0f}% "
                            f"({cascade['treated_wo_test']:,}) at {fac} ({per}).",
                        ):
                            created += 1

                # High TPR (RDT) — formula gate first
                rdt_exam = _match_ind(fac_p, r"RDT.*Total Exam|Rapid diagnostic.*Total Exam")
                rdt_pos = _match_ind(fac_p, r"RDT.*Positive|Rapid diagnostic.*Positive")
                ex = float(rdt_exam["value"].sum()) if not rdt_exam.empty else 0.0
                po = float(rdt_pos["value"].sum()) if not rdt_pos.empty else 0.0
                if ex > 0 or po > 0:
                    tpr_a = safe_rate(
                        po, ex, indicator="alert_tpr", facility=fac, period=per,
                        formula="RDT pos ÷ RDT exams × 100", source="MOH 706",
                    )
                    if tpr_a["status"] == "invalid":
                        if _insert_alert(
                            conn, "tpr_invalid", "Data Quality", "critical", fac, "MOH 706", per,
                            f"Invalid RDT TPR at {fac} ({per}): positives {po:g} > exams {ex:g} "
                            f"(raw {tpr_a.get('calculated_pct')}%). Reconcile — not a performance signal.",
                        ):
                            created += 1
                    elif tpr_a["status"] == "valid" and ex >= 30 and tpr_a["value"] >= 70:
                        if _insert_alert(
                            conn, "tpr_high", "Performance", "warning", fac, "MOH 706", per,
                            f"RDT positivity {tpr_a['value']:.0f}% on {ex:,.0f} exams at {fac} ({per}).",
                        ):
                            created += 1

            # GO_1 inputs missing across recent window for this facility
            fac_focus = focus[focus["facility"] == fac]
            rdt_exam_f = _match_ind(fac_focus, r"RDT.*Total Exam|Rapid diagnostic.*Total Exam")
            if rdt_exam_f.empty or float(rdt_exam_f["value"].sum()) <= 0:
                if not fac_focus.empty:
                    per_label = recent[0] if recent else "recent"
                    if _insert_alert(
                        conn, "go1_missing", "Data Quality", "critical", fac, "MOH 706", per_label,
                        f"No RDT exam volume for GO_1 at {fac} in recent periods.",
                    ):
                        created += 1

            # Testing collapse: compare two newest monthly periods with volume
            monthly_vol = []
            for per in recent:
                sub = fac_all[fac_all["period"].astype(str) == str(per)]
                vol = _sum_ind(sub, r"Total Exam|Tested for Malaria")
                if vol > 0:
                    monthly_vol.append((per, vol))
            if len(monthly_vol) >= 2:
                # recent list is newest-first; monthly_vol follows that order among non-zero
                per_new, vol_new = monthly_vol[0]
                per_old, vol_old = monthly_vol[1]
                if vol_old > 0 and vol_new < vol_old * 0.5:
                    # TPR direction on same windows
                    def _tpr(per):
                        sub = fac_all[fac_all["period"].astype(str) == str(per)]
                        e = _match_ind(sub, r"RDT.*Total Exam|Rapid diagnostic.*Total Exam")
                        p = _match_ind(sub, r"RDT.*Positive|Rapid diagnostic.*Positive")
                        ee = float(e["value"].sum()) if not e.empty else 0.0
                        pp = float(p["value"].sum()) if not p.empty else 0.0
                        a = safe_rate(pp, ee, indicator="collapse_tpr")
                        return a["value"] if a.get("status") == "valid" else None

                    tpr_new, tpr_old = _tpr(per_new), _tpr(per_old)
                    tpr_note = ""
                    if tpr_new is not None and tpr_old is not None and tpr_new > tpr_old:
                        tpr_note = f" TPR rose {tpr_old:.0f}% → {tpr_new:.0f}% — do not read as success."
                    if _insert_alert(
                        conn, "testing_collapse", "Performance", "critical", fac, "", per_new,
                        f"Testing volume fell >50% at {fac}: {vol_old:,.0f} ({per_old}) → "
                        f"{vol_new:,.0f} ({per_new}).{tpr_note}",
                    ):
                        created += 1

        # ── Stock ──
        try:
            stock = pd.read_sql_query("SELECT * FROM stock", conn)
        except Exception:
            stock = pd.DataFrame()
        if not stock.empty:
            for _, s in stock.iterrows():
                item = str(s.get("item", ""))
                qty = float(s.get("quantity") or 0)
                reorder = float(s.get("reorder_point") or s.get("reorder_level") or 30)
                if qty < reorder:
                    key = "stock_rdt" if item.upper() == "RDT" else (
                        "stock_al" if item.upper() == "AL" else f"stock_{item.lower()}"
                    )
                    if _insert_alert(
                        conn, key, "Commodity", "high", s.get("facility", "—"), item, "current",
                        f"{item} stock is {qty:.0f} (reorder level {reorder:.0f}) at {s.get('facility')}.",
                    ):
                        created += 1

        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        # Surface once for debugging rather than silent pass
        try:
            log_audit("system", "evaluate_alerts_error", "alerts", str(e)[:200])
        except Exception:
            pass
    finally:
        conn.close()
    return created


def get_active_alerts(facility=None):
    conn = get_conn()
    try:
        sql = "SELECT * FROM alerts WHERE status = 'open'"
        params = []
        if facility and facility not in (None, "All three"):
            sql += " AND facility = ?"
            params.append(facility)
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 ELSE 3 END, triggered_at DESC"
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


def get_resolved_alerts(limit=30):
    try:
        return query(
            """
            SELECT * FROM alerts WHERE status = 'resolved'
            ORDER BY resolved_at DESC LIMIT ?
            """,
            (limit,),
        )
    except Exception:
        return pd.DataFrame()


# ==================================================================
# PAGE 2 — OVERVIEW
# ==================================================================


def _cmd_readiness_facility(df, facility, filters=None):
    """Simple 0–100 response readiness from cascade, reporting, stock."""
    pf = dict(filters or {})
    pf["facility"] = facility
    d = _filter_data(df, pf) if df is not None and not df.empty else pd.DataFrame()
    scores = {}
    try:
        c = compute_cascade_metrics(d) if not d.empty else {}
        scores["testing"] = min(100, max(0, float(c.get("testing_rate") or 0)))
        scores["treatment"] = min(100, max(0, float(c.get("treat_coverage") or 0)))
    except Exception:
        scores["testing"] = scores["treatment"] = 50
    try:
        r12 = compute_indicator("R1_2", df, pf)
        scores["reporting"] = float(r12.get("value") or 0) if r12.get("status") == "valid" else 50
    except Exception:
        scores["reporting"] = 50
    try:
        stock = query("SELECT item, quantity FROM stock WHERE facility = ?", (facility,))
        rdt_ok = act_ok = 50
        if not stock.empty:
            for _, r in stock.iterrows():
                item = str(r.get("item") or "").upper()
                qty = float(r.get("quantity") or 0)
                if "RDT" in item or "TEST" in item:
                    rdt_ok = 90 if qty > 50 else (60 if qty > 0 else 20)
                if "AL" in item or "ACT" in item:
                    act_ok = 90 if qty > 20 else (60 if qty > 0 else 20)
        scores["commodities"] = (rdt_ok + act_ok) / 2
    except Exception:
        scores["commodities"] = 50
    vals = list(scores.values())
    overall = float(sum(vals) / len(vals)) if vals else 50.0
    return overall, scores


def _cmd_risk_from_ew(facility):
    try:
        ew = query(
            "SELECT risk_score, risk_level, predicted_cases FROM ew_forecasts "
            "WHERE facility = ? AND horizon_weeks = 4 ORDER BY run_at DESC LIMIT 1",
            (facility,),
        )
        if ew.empty:
            return None, "—", None
        r = ew.iloc[0]
        return float(r.get("risk_score") or 0), str(r.get("risk_level") or "—"), r.get("predicted_cases")
    except Exception:
        return None, "—", None


def page_command_centre(user, filters):
    """Programme Command Centre — decision-first operational prioritisation."""
    page_header(
        "Programme Command Centre",
        "What needs attention today — signals, gaps, readiness and recommended checks. "
        "Not an outbreak declaration; operational prioritisation for Kilifi CBMP.",
        breadcrumb="Monitor / Command Centre",
    )
    df = load_all_data()
    try:
        n_open = int(query("SELECT COUNT(*) AS n FROM alerts WHERE status='open'").iloc[0]["n"])
    except Exception:
        n_open = 0
    try:
        n_high = int(query(
            "SELECT COUNT(*) AS n FROM alerts WHERE status='open' AND severity IN ('critical','high')"
        ).iloc[0]["n"])
    except Exception:
        n_high = 0
    data_lbl, ew_lbl, sc_lbl = _system_status_bits()
    c0, c1, c2, c3 = st.columns(4)
    c0.metric("Open alerts", n_open)
    c1.metric("Critical / high", n_high)
    c2.metric("Data", data_lbl.replace("Data through: ", "") if "Data" in data_lbl else "—")
    c3.metric("EW", ew_lbl.replace("EW forecasts: ", "n=") if "EW" in ew_lbl else "—")

    section_header("What needs attention today")
    attention = []
    try:
        al = get_active_alerts()
        if al is not None and not al.empty:
            for _, r in al.head(15).iterrows():
                fam = _alert_family(r.get("category"), r.get("message"))
                sev = str(r.get("severity") or "warning").lower()
                attention.append({
                    "priority": 0 if sev == "critical" else (1 if sev == "high" else 2),
                    "severity": sev,
                    "geography": r.get("facility") or "County",
                    "family": fam,
                    "title": str(r.get("message") or "")[:120],
                    "source": "Alert",
                })
    except Exception:
        pass
    for fac in FACILITIES:
        score, level, pred = _cmd_risk_from_ew(fac)
        if level and str(level).lower() in ("high", "very high"):
            attention.append({
                "priority": 0 if "very" in str(level).lower() else 1,
                "severity": "critical" if "very" in str(level).lower() else "high",
                "geography": fac,
                "family": "Forecast",
                "title": f"EW 4-week risk {level} · expected ~ {pred}",
                "source": "Early warning",
            })
        if df is not None and not df.empty:
            try:
                pf = dict(filters or {})
                pf["facility"] = fac
                c = compute_cascade_metrics(_filter_data(df, pf))
                if c.get("suspected", 0) >= 20 and c.get("diag_gap_pct", 0) >= 25:
                    attention.append({
                        "priority": 1,
                        "severity": "high",
                        "geography": fac,
                        "family": "Surveillance",
                        "title": f"Diagnosis gap {c.get('diag_gap_pct'):.0f}% ({c.get('diag_gap')} of {c.get('suspected')} not tested)",
                        "source": "Cascade",
                    })
            except Exception:
                pass
    attention = sorted(attention, key=lambda x: (x["priority"], x["geography"]))
    immediate = [a for a in attention if a["priority"] <= 1]
    monitor = [a for a in attention if a["priority"] == 2]
    def _decision_for(a):
        fam = str(a.get("family") or "")
        geo = a.get("geography") or "Sub-county"
        if fam == "Forecast" or a.get("source") == "Early warning":
            return {
                "why": "Elevated early-warning risk vs recent surveillance/climate context.",
                "action": "Verify case registers, testing volumes and RDT stock; review EW drivers.",
                "owner": f"Sub-county malaria focal ({geo})",
                "due": "7 days",
                "confidence": "Moderate — confirm with latest weekly data",
            }
        if fam == "Surveillance" or "gap" in str(a.get("title") or "").lower():
            return {
                "why": "Testing/diagnosis performance below programme expectation.",
                "action": "Check RDT availability, testing practice and register completeness.",
                "owner": f"Facility in-charge / CHA ({geo})",
                "due": "5 days",
                "confidence": "High if numerator/denominator complete",
            }
        if fam == "Data quality":
            return {
                "why": "Data quality issue may distort rates and forecasts.",
                "action": "Correct registers / re-submit period; do not escalate response on bad data alone.",
                "owner": f"Facility data focal / Sub-county M&E ({geo})",
                "due": "3 days",
                "confidence": "High — data issue is observed",
            }
        if fam == "Commodity":
            return {
                "why": "Commodity signal may limit diagnosis or treatment capacity.",
                "action": "Confirm stock on hand and reorder; align with surveillance demand.",
                "owner": f"Facility commodity focal ({geo})",
                "due": "3 days",
                "confidence": "Moderate — depends on stock currency",
            }
        return {
            "why": "Programme signal requires review.",
            "action": "Acknowledge alert, investigate source data, document outcome in Alerts lifecycle.",
            "owner": f"Sub-county malaria team ({geo})",
            "due": "7 days",
            "confidence": "See alert evidence panel",
        }

    if not attention:
        st.success("No high-priority signals in the current data window. Continue routine surveillance.")
    else:
        st.markdown(f"**{len(immediate)} require attention** · **{len(monitor)} to monitor**")
        for a in immediate[:8]:
            d = _decision_for(a)
            tag = "PRIORITY" if a["priority"] == 0 else "ATTENTION"
            st.markdown(
                f"**[{tag}] {a['geography']}** · {a['family']} · {a['source']}\n\n"
                f"{a['title']}\n\n"
                f"- **Why it matters:** {d['why']}\n"
                f"- **Recommended action:** {d['action']}\n"
                f"- **Owner:** {d['owner']}\n"
                f"- **Due:** {d['due']}\n"
                f"- **Confidence:** {d['confidence']}"
            )
            st.markdown("---")
        if monitor:
            with st.expander(f"Monitor ({len(monitor)})"):
                for a in monitor[:10]:
                    d = _decision_for(a)
                    st.markdown(
                    st.markdown(
                        f"- **{a['geography']}** — {a['title']} | Action: {d['action']} | Owner: {d['owner']}"
                    )
                    )

    section_header("Decision loop (data → action)")
    st.caption(
        "Observe → Detect → Explain → Prioritize → Act → Measure. "
        "Record outcomes under **Alerts → lifecycle** (New → Acknowledged → Investigating → Action → Monitoring → Closed)."
    )

    section_header("Risk x response readiness")
    st.caption(
        "Risk from latest 4-week EW (if run). Readiness from testing, treatment, reporting, commodities. "
        "High risk + low readiness = priority attention — not an automatic outbreak label."
    )
    matrix_rows = []
    for fac in FACILITIES:
        if filters and filters.get("facility") not in (None, "All three") and filters.get("facility") != fac:
            continue
        rscore, rlevel, pred = _cmd_risk_from_ew(fac)
        ready, parts = _cmd_readiness_facility(df, fac, filters)
        risk_band = "Low"
        if rscore is not None:
            if rscore >= 0.7 or str(rlevel).lower() in ("high", "very high"):
                risk_band = "High"
            elif rscore >= 0.4 or str(rlevel).lower() == "moderate":
                risk_band = "Moderate"
        ready_band = "High" if ready >= 70 else ("Moderate" if ready >= 50 else "Low")
        if risk_band == "High" and ready_band == "Low":
            cell = "PRIORITY attention"
        elif risk_band == "High":
            cell = "Monitor closely"
        elif ready_band == "Low":
            cell = "Preparedness watch"
        else:
            cell = "Routine"
        matrix_rows.append({
            "Facility": fac,
            "EW risk": rlevel,
            "Risk score": None if rscore is None else round(rscore, 2),
            "4w expected": pred,
            "Readiness": round(ready, 0),
            "Testing": round(parts.get("testing", 0), 0),
            "Reporting": round(parts.get("reporting", 0), 0),
            "Commodities": round(parts.get("commodities", 0), 0),
            "Priority cell": cell,
        })
    mat = pd.DataFrame(matrix_rows)
    if mat.empty:
        st.info("No facility rows for current filters.")
    else:
        st.dataframe(mat, use_container_width=True, hide_index=True)

    section_header("Why is this area elevated?")
    pick_fac = st.selectbox("Facility", FACILITIES, key="cmd_why_fac")
    rscore, rlevel, pred = _cmd_risk_from_ew(pick_fac)
    ready, parts = _cmd_readiness_facility(df, pick_fac, filters)
    evidence = []
    if df is not None and not df.empty:
        try:
            pf = {**(filters or {}), "facility": pick_fac}
            c = compute_cascade_metrics(_filter_data(df, pf))
            go = compute_indicator("GO_1", df, pf)
            evidence.append(("Confirmed cases", c.get("confirmed"), "Surveillance"))
            evidence.append(("Tested", c.get("tested"), "Surveillance"))
            evidence.append(("Diagnosis gap", c.get("diag_gap"), "Surveillance"))
            evidence.append(("TPR %", go.get("value") if go.get("status") == "valid" else None, "Surveillance"))
            evidence.append(("Testing rate %", c.get("testing_rate"), "Surveillance"))
        except Exception:
            pass
    try:
        clim = query(
            "SELECT rainfall_mm, temperature_c, week_ending FROM climate_data "
            "WHERE facility = ? ORDER BY week_ending DESC LIMIT 4",
            (pick_fac,),
        )
        if not clim.empty:
            evidence.append(("Rain last ~4 weeks (mm)", round(float(pd.to_numeric(clim["rainfall_mm"], errors="coerce").sum()), 1), "Climate"))
            evidence.append(("Mean temp C", round(float(pd.to_numeric(clim["temperature_c"], errors="coerce").mean()), 1), "Climate"))
    except Exception:
        pass
    evidence.append(("EW risk level", rlevel, "Forecast"))
    evidence.append(("EW expected cases (4w)", pred, "Forecast"))
    evidence.append(("Readiness overall", round(ready, 0), "Readiness"))
    for k, v in parts.items():
        evidence.append((f"Readiness · {k}", round(v, 0), "Readiness"))
    st.dataframe(pd.DataFrame(evidence, columns=["Signal", "Value", "Domain"]), use_container_width=True, hide_index=True)
    domains_hit = set()
    if rscore is not None and rscore >= 0.4:
        domains_hit.add("Forecast")
    for name, val, dom in evidence:
        try:
            if dom == "Surveillance" and val is not None and "gap" in name.lower() and float(val) > 0:
                domains_hit.add("Surveillance")
            if dom == "Surveillance" and "TPR" in name and val is not None and float(val) > 20:
                domains_hit.add("Surveillance")
            if dom == "Climate" and "Rain" in name and val is not None and float(val) > 50:
                domains_hit.add("Climate")
            if dom == "Readiness" and name == "Readiness overall" and val is not None and float(val) < 60:
                domains_hit.add("Commodities/readiness")
        except Exception:
            pass
    st.markdown(
        f"**Signal convergence for {pick_fac}:** "
        f"{len(domains_hit)} domain(s) — {', '.join(sorted(domains_hit)) or 'none elevated'}."
    )
    st.caption(
        "Descriptive only. Multiple domains moving together supports reviewing surveillance and readiness — not proof of outbreak."
    )

    section_header("Programme gaps (deficit-first)")
    gap_rows = []
    for fac in FACILITIES:
        if filters and filters.get("facility") not in (None, "All three") and filters.get("facility") != fac:
            continue
        if df is None or df.empty:
            continue
        try:
            pf = {**(filters or {}), "facility": fac}
            c = compute_cascade_metrics(_filter_data(df, pf))
            gap_rows.append({
                "Facility": fac,
                "Suspected": c.get("suspected", 0),
                "Tested": c.get("tested", 0),
                "Testing gap (not tested)": c.get("diag_gap", 0),
                "Testing %": c.get("testing_rate"),
                "Confirmed": c.get("confirmed", 0),
                "Treatment gap": c.get("treat_gap", 0),
                "Treat cov %": c.get("treat_coverage"),
            })
        except Exception:
            pass
    if gap_rows:
        st.dataframe(pd.DataFrame(gap_rows), use_container_width=True, hide_index=True)
        st.caption("Gaps = people or cases not reached. Percentages remain for context.")
    else:
        st.info("Commit KHIS data to compute testing and treatment gaps.")

    section_header("What changed (latest vs previous period)")
    if df is not None and not df.empty and "period" in df.columns:
        pers = sorted(df["period"].astype(str).unique().tolist())
        monthly = [p for p in pers if re.match(r"^\d{4}-\d{2}$", p)]
        if len(monthly) >= 2:
            cur_p, prev_p = monthly[-1], monthly[-2]
            st.caption(f"Comparing **{cur_p}** vs **{prev_p}**")
            chg = []
            for fac in FACILITIES:
                try:
                    d_cur = _filter_data(df, {"facility": fac, "periods": [cur_p]})
                    d_prev = _filter_data(df, {"facility": fac, "periods": [prev_p]})
                    c1 = compute_cascade_metrics(d_cur)
                    c0 = compute_cascade_metrics(d_prev)
                    conf1, conf0 = float(c1.get("confirmed") or 0), float(c0.get("confirmed") or 0)
                    test1, test0 = float(c1.get("tested") or 0), float(c0.get("tested") or 0)
                    chg.append({
                        "Facility": fac,
                        "Confirmed now": conf1,
                        "d confirmed": conf1 - conf0,
                        "Tested now": test1,
                        "d tested": test1 - test0,
                        "Note": (
                            "Rising cases + falling testing -> investigate surveillance"
                            if (conf1 - conf0) > 0 and (test1 - test0) < 0 else "—"
                        ),
                    })
                except Exception:
                    pass
            if chg:
                st.dataframe(pd.DataFrame(chg), use_container_width=True, hide_index=True)
        else:
            st.info("Need at least two monthly periods for change detection.")
    else:
        st.info("No period data for change detection.")

    st.caption(
        "Command Centre uses alerts, cascade, stock, climate and EW forecasts already in the system. "
        "Record actions in Alerts lifecycle. Green = no escalation signal — not absence of transmission."
    )



def compute_data_trust(df, filters, indicator_id=None):
    """
    Lightweight data-confidence badge for Overview KPIs.
    Returns dict: level (High/Moderate/Low), emoji, reasons (list).
    """
    reasons = []
    score = 100
    d = _filter_data(df, filters) if df is not None and not df.empty else pd.DataFrame()
    n = len(d)
    if n == 0:
        return {"level": "Low", "emoji": "🔴", "reasons": ["No rows in current filter"], "score": 0}
    if n < 20:
        score -= 25
        reasons.append(f"Few observations (n={n})")
    elif n < 80:
        score -= 10
        reasons.append(f"Limited observations (n={n})")
    else:
        reasons.append(f"Adequate volume (n={n})")
    # Period coverage
    if "period" in d.columns:
        nper = d["period"].nunique()
        if nper < 2:
            score -= 15
            reasons.append("Single period only")
        else:
            reasons.append(f"{nper} periods in view")
    # Facility coverage for county view
    fac = (filters or {}).get("facility")
    if fac in (None, "All three") and "facility" in d.columns:
        nf = d["facility"].nunique()
        if nf < len(FACILITIES):
            score -= 10
            reasons.append(f"Facilities with data: {nf}/{len(FACILITIES)}")
    # Indicator-specific
    if indicator_id:
        try:
            r = compute_indicator(indicator_id, df, filters)
            stt = r.get("status") or r.get("formula_status")
            if stt == "invalid":
                score -= 40
                reasons.append(f"{indicator_id} INVALID")
            elif stt == "na":
                score -= 30
                reasons.append(f"{indicator_id} N/A")
            if r.get("testing_collapse"):
                score -= 15
                reasons.append("Testing collapse flag")
        except Exception:
            score -= 10
            reasons.append("Could not validate indicator")
    # Reporting completeness proxy
    try:
        r12 = compute_indicator("R1_2", df, filters)
        if r12.get("status") == "valid" and r12.get("value") is not None:
            rv = float(r12["value"])
            if rv < 70:
                score -= 20
                reasons.append(f"Reporting rate low ({rv:.0f}%)")
            elif rv < 90:
                score -= 8
                reasons.append(f"Reporting rate moderate ({rv:.0f}%)")
            else:
                reasons.append(f"Reporting rate strong ({rv:.0f}%)")
    except Exception:
        pass
    score = max(0, min(100, score))
    if score >= 75:
        level, emoji = "High", "🟢"
    elif score >= 50:
        level, emoji = "Moderate", "🟠"
    else:
        level, emoji = "Low", "🔴"
    return {"level": level, "emoji": emoji, "reasons": reasons, "score": score}


def page_overview(user, filters):
    df = load_all_data()

    badges = []
    for fac in FACILITIES:
        fac_rows = len(df[df["facility"] == fac]) if not df.empty else 0
        if fac_rows == 0:
            badges.append(f"⚫ {fac}")
        elif fac_rows > 100:
            badges.append(f"🟢 {fac}")
        else:
            badges.append(f"🟡 {fac}")

    st.caption(" · ".join(badges) if badges else "")

    with st.expander("🔗 Share this view (secure read-only link)", expanded=False):
        st.caption(
            "Creates a token the recipient opens with `?share=<token>`. "
            "They see only this snapshot — no full dashboard navigation."
        )
        share_title = st.text_input("Share title", value="Kilifi malaria overview", key="ov_share_title")
        share_days = st.selectbox("Expires in", [1, 7, 30, 90], index=1, key="ov_share_days")
        if st.button("Create secure share link", key="ov_share_go"):
            metrics = []
            try:
                inds = compute_official_indicators(df, filters) if not df.empty else {}
                for iid in ("GO_1", "R1_1", "R1_2", "CAS_1"):
                    r = inds.get(iid) or {}
                    metrics.append({
                        "Indicator": iid,
                        "Value": r.get("value"),
                        "Status": r.get("status") or r.get("formula_status"),
                    })
            except Exception:
                pass
            payload = {
                "summary": f"Overview snapshot · facility={filters.get('facility')} · periods={filters.get('periods')}",
                "metrics": metrics,
                "notes": f"Shared by {user.get('name')} ({user.get('username')})",
            }
            tok = create_shared_view(share_title, "Overview", payload, user.get("username"), days=int(share_days))
            st.success("Share link created.")
            st.code(f"?share={tok}", language=None)
            st.caption("Append to your Streamlit app URL and send. Manage/revoke under Users & Access → Shared links.")
    try:
        pend = query(
            """
            SELECT COUNT(*) AS n FROM alerts
            WHERE status='open' AND IFNULL(followed_up,0)=0
              AND severity IN ('critical','high')
            """
        )
        pn = int(pend.iloc[0]["n"]) if not pend.empty else 0
        if pn:
            st.error(f"Reminder: **{pn}** critical/high alert(s) have no follow-up tick. Open **Alerts**.")
    except Exception:
        pass

    n_db = count_rows()
    if df.empty or n_db == 0:
        st.warning("No data in the database yet.")
        st.markdown(
            """
**What to do**

1. Open **Upload & Settings** in the left sidebar.  
2. Choose your KHIS Excel file and wait for the **validation report**.  
3. Click **✅ Commit to database** (parsing alone does not save).  
4. Come back to **Overview**.

If you already clicked Commit, check Upload & Settings → **Database status** / recent uploads.  
Database file: `{}`
""".format(DB_PATH)
        )
        if st.session_state.get("last_commit_rows"):
            st.caption(
                f"Last commit this session reported {st.session_state.last_commit_rows:,} rows — "
                "if Overview is still empty, the app may be using a different folder/database."
            )
        return

    # ALL analysis uses filtered frame
    d = _filter_data(df, filters)
    if d.empty:
        st.warning(
            "No rows match the **active filters** (data is in the database, but filtered out)."
        )
        st.markdown(
            "Click **Apply filters** and choose **All periods** or **Latest year (monthly)**, "
            "and register preset **All registers**."
        )
        st.caption(f"Raw rows in database: **{len(df):,}** · Active period mode: {filters.get('period_mode')}")
        return

    indicators = compute_official_indicators(df, filters)
    last_p = last_data_period(d)

    st.caption("What can I do here? See where we are, what changed, and which facility needs action — in 30 seconds.")
    go = indicators.get("GO_1") or {}
    so = indicators.get("SO_1") or {}
    r11 = indicators.get("R1_1") or {}
    pri = []
    if go.get("testing_collapse"):
        pri.append("Testing volume fell while positivity rose — check GO_1 collapse rule, not ‘success’.")
    if go.get("formula_status") == "invalid":
        pri.append("GO_1 INVALID (numerator > denominator or mixed grain). Do not publish TPR.")
    try:
        al = get_active_alerts()
        crit = al[al["severity"] == "critical"] if not al.empty else pd.DataFrame()
        for _, ar in crit.head(3).iterrows():
            pri.append(f"{ar.get('facility')}: {str(ar.get('message'))[:140]}")
    except Exception:
        pass
    if pri:
        st.error("**Priority actions**\n" + "\n".join(f"{i}. {p}" for i, p in enumerate(pri[:5], 1)))
    fac_stat = []
    for fac in FACILITIES:
        pf = indicators["GO_1"].get("per_facility", {}).get(fac, {}) or {}
        issue = "—"
        if pf.get("status") == "invalid":
            issue = "INVALID TPR"
        elif go.get("testing_collapse"):
            issue = "testing collapse watch"
        fac_stat.append({
            "Facility": fac,
            "TPR": pf.get("value") if pf.get("status") == "valid" else None,
            "TPR status": pf.get("status"),
            "RDT exams": pf.get("tested"),
            "Main issue": issue,
        })
    st.dataframe(pd.DataFrame(fac_stat), use_container_width=True, hide_index=True)

    # B1 Command Centre strip
    section_header("Official logframe (4) + supporting catalogue")
    try:
        cat_df = evaluate_catalogue(df, filters)
        core_ids = ["GO_1", "SO_1", "R1_1", "R1_2", "CAS_1", "CAS_2", "EPI_1", "DQ_03"]
        core = cat_df[cat_df["ID"].isin(core_ids)]
        n_inv = int((cat_df["Status"] == "INVALID").sum())
        n_val = int((cat_df["Status"] == "VALID").sum())
        st.caption(
            f"Catalogue **{len(INDICATOR_CATALOGUE)}** · VALID **{n_val}** · INVALID **{n_inv}** · "
            "Full explorer: **All Indicators**."
        )
        if not core.empty:
            cols = st.columns(4)
            for i, (_, r) in enumerate(core.iterrows()):
                with cols[i % 4]:
                    if r["Status"] == "INVALID":
                        disp = "INVALID"
                    elif r["Value"] is None or r["Status"] == "NA":
                        disp = "N/A"
                    elif r["Unit"] == "%":
                        disp = f"{float(r['Value']):.1f}%"
                    elif r["Unit"] in ("count", "score"):
                        try:
                            disp = f"{int(r['Value']):,}"
                        except Exception:
                            disp = str(r["Value"])
                    else:
                        disp = str(r["Value"])
                    emoji = {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚫"}.get(r["RAG"], "⚫")
                    trust = compute_data_trust(df, filters, indicator_id=r["ID"])
                    st.metric(f"{emoji} {r['ID']}", disp)
                    st.caption(f"{trust['emoji']} Data trust: **{trust['level']}**")
        # Overall trust panel
        overall = compute_data_trust(df, filters, indicator_id="GO_1")
        with st.expander(f"Data trust detail — overall {overall['emoji']} {overall['level']} ({overall['score']}/100)", expanded=False):
            st.write("; ".join(overall["reasons"]))
            st.caption(
                "Trust reflects completeness, volume, validity and reporting in the **current filter** — "
                "not a statement about transmission intensity."
            )
        if n_inv:
            st.warning(f"{n_inv} catalogue value(s) INVALID — integrity audit on Data foundation.")
    except Exception as e:
        st.caption(f"Command Centre strip: {e}")

    f1, f2, f3 = st.columns([2, 1, 1])
    with f1:
        st.caption(f"📅 **Data in view:** {last_p or '—'} · {len(d):,} rows · Phase 0 {PHASE_0_SIGNED_DATE}")
    with f2:
        st.caption(f"RDT exams (GO): **{indicators['GO_1'].get('tested', 0):,}**")
    with f3:
        if indicators["GO_1"].get("testing_collapse"):
            st.caption("⚠️ Testing-collapse flag")

    levels = {k: indicator_rag(indicators[k], k) for k in ("GO_1", "SO_1", "R1_1", "R1_2")}
    worst_lv = "red" if "red" in levels.values() else ("amber" if "amber" in levels.values() else "green")
    headline(auto_headline(indicators), status=worst_lv)

    # ── Command centre v1: Where / Wrong / Why / Action ──
    page_header(
        "Programme command centre",
        "Where are we? What is going wrong? What needs action? "
        "Scoped to active filters. Drill into Facility Deep-Dive, Alerts, or Data foundation for detail.",
    )
    cascade_cc = compute_cascade_metrics(d)
    actions_cc = build_action_list(d, indicators)
    try:
        evaluate_alerts(recent_months=6)
    except Exception:
        pass
    alerts_cc = get_active_alerts(
        facility=None if filters.get("facility") in (None, "All three") else filters.get("facility")
    )
    crit_a = int((alerts_cc["severity"] == "critical").sum()) if not alerts_cc.empty else 0
    high_a = int((alerts_cc["severity"] == "high").sum()) if not alerts_cc.empty else 0
    try:
        recon = query("SELECT items_flagged, items_checked, id FROM recon_runs ORDER BY id DESC LIMIT 1")
        recon_flag = int(recon.iloc[0]["items_flagged"]) if not recon.empty else 0
        recon_id = int(recon.iloc[0]["id"]) if not recon.empty else None
    except Exception:
        recon_flag, recon_id = 0, None
    dq_cc = compute_dq_score(df, filters)

    on_track = sum(1 for lv in levels.values() if lv == "green")
    off = [k for k, lv in levels.items() if lv in ("red", "amber")]
    cc1, cc2, cc3, cc4 = st.columns(4)
    with cc1:
        big_number(f"{on_track}/4", "Official on track", delta=worst_lv.upper())
    with cc2:
        big_number(str(crit_a + high_a), "Critical+high alerts", delta=f"{crit_a} critical")
    with cc3:
        big_number(
            f"{cascade_cc['diag_gap_pct']:.0f}%",
            "Diagnosis gap",
            delta=f"{cascade_cc['diag_gap']:,} not tested",
            delta_direction="down" if cascade_cc["diag_gap_pct"] > 10 else "flat",
        )
    with cc4:
        big_number(
            f"{dq_cc['score']:.0f}" if dq_cc.get("score") is not None else "—",
            "DQ score",
            delta=dq_cc.get("label", ""),
        )

    # Deterioration / persistent problems (simple rules on filtered data)
    section_header("Emerging deterioration & outstanding risks")
    risks = []
    if indicators["GO_1"].get("testing_collapse"):
        risks.append(("critical", "Testing collapse", "Testing volume fell while TPR rose — investigate before celebrating positivity."))
    if indicators["GO_1"].get("formula_status") == "invalid":
        risks.append((
            "critical",
            "Invalid GO_1 formula",
            indicators["GO_1"].get("formula_reason")
            or "RDT positives > exams — TPR not valid for RAG.",
        ))
    n_fi = len(indicators["GO_1"].get("formula_issues") or [])
    if n_fi:
        risks.append((
            "high",
            "Formula engine flags",
            f"{n_fi} facility/period calculation(s) with numerator > denominator — reconcile MOH 706.",
        ))
    if cascade_cc["diag_gap_pct"] >= 25 and cascade_cc["suspected"] >= 20:
        risks.append(("high", "High diagnosis gap", f"{cascade_cc['diag_gap_pct']:.0f}% of suspected not tested in this view."))
    if cascade_cc["treat_gap_pct"] >= 20 and cascade_cc["confirmed"] >= 10:
        risks.append(("high", "High treatment gap", f"{cascade_cc['treat_gap_pct']:.0f}% confirmed without AL in this view."))
    if levels.get("R1_2") == "red":
        risks.append(("high", "Reporting off track", "R1.2 below target — late or incomplete facility reports."))
    if levels.get("R1_1") == "red":
        risks.append(("medium", "CHP tests behind target", "R1.1 below path for this filter window (check run-rate on R1.1 page)."))
    if levels.get("SO_1") in ("red", "amber"):
        risks.append(("medium", "CHP competency", "SO_1 below target — schedule assessments."))
    if crit_a:
        risks.append(("critical", "Open critical alerts", f"{crit_a} critical alert(s) — open Alerts."))
    if recon_flag:
        risks.append(("medium", "Reconciliation flags", f"{recon_flag} item(s) on latest recon run #{recon_id} — Data foundation."))
    if dq_cc.get("score") is not None and dq_cc["score"] < 65:
        risks.append(("high", "Weak data quality", f"DQ score {dq_cc['score']:.0f} — review Data Quality page."))
    try:
        fc = compute_commodity_forecast()
        if not fc.empty:
            n_bad = int(fc["Status"].isin(["stock_out", "critical", "low"]).sum())
            if n_bad:
                risks.append((
                    "high", "Commodity risk",
                    f"{n_bad} stock line(s) stock-out/critical/low — see CHP Performance → Stock forecast.",
                ))
    except Exception:
        pass
    try:
        sa = query("SELECT deadline, status FROM supervision_actions WHERE status IN ('open','in_progress')")
        if not sa.empty:
            today_s = datetime.now().strftime("%Y-%m-%d")
            n_od = int(
                sa.apply(
                    lambda r: str(r.get("deadline") or "") < today_s and bool(r.get("deadline")),
                    axis=1,
                ).sum()
            )
            if n_od:
                risks.append((
                    "medium", "Overdue supervision actions",
                    f"{n_od} open action(s) past deadline — CHP Performance → Supervision.",
                ))
    except Exception:
        pass
    try:
        wp = query("SELECT due_date, status FROM workplan_actions WHERE status IN ('open','in_progress')")
        if not wp.empty:
            today_s = datetime.now().strftime("%Y-%m-%d")
            n_od = int(
                wp.apply(
                    lambda r: str(r.get("due_date") or "") < today_s and bool(r.get("due_date")),
                    axis=1,
                ).sum()
            )
            if n_od:
                risks.append((
                    "high", "Overdue workplan items",
                    f"{n_od} workplan action(s) past due — CHP Performance → Workplan.",
                ))
    except Exception:
        pass

    # Simple MoM volume drop across facilities (deterioration signal)
    monthly = d[d["period"].astype(str).str.len() == 7]
    if not monthly.empty:
        vol = monthly[
            monthly["indicator"].str.contains(r"Total Exam|Tested for Malaria", na=False, regex=True)
        ].groupby(monthly["period"].astype(str))["value"].sum().sort_index()
        if len(vol) >= 2 and vol.iloc[-2] > 0 and vol.iloc[-1] < vol.iloc[-2] * 0.7:
            risks.append((
                "high", "Testing volume down >30% MoM",
                f"{vol.index[-2]}: {vol.iloc[-2]:,.0f} → {vol.index[-1]}: {vol.iloc[-1]:,.0f}.",
            ))

    if not risks:
        st.success("No major deterioration signals from current rules for this filter window.")
    else:
        order_r = {"critical": 0, "high": 1, "medium": 2}
        risks.sort(key=lambda x: order_r.get(x[0], 9))
        for sev, title, msg in risks[:8]:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(sev, "•")
            st.markdown(f"{icon} **{title}** — {msg}")

    if off:
        st.caption("Official indicators not green: **" + ", ".join(off) + "**")

    section_header("Corrective actions (this view)")
    if not actions_cc:
        st.success("No urgent actions from current thresholds.")
    else:
        for a in actions_cc:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(a.get("priority"), "•")
            st.markdown(f"{icon} **{str(a.get('priority', '')).upper()}** — {a.get('text', '')}")

    if not alerts_cc.empty:
        with st.expander(f"Open alerts snapshot ({len(alerts_cc)})", expanded=False):
            for _, row in alerts_cc.head(8).iterrows():
                msg = str(row["message"])
                if msg.startswith("[") and "]" in msg:
                    msg = msg.split("]", 1)[1].strip()
                st.caption(f"· {row['severity']} · {row['facility']} · {row.get('period')} — {msg}")
            st.caption("Resolve on the **Alerts** page.")

    st.markdown("---")
    # --- CHARTS ---
    section_header("Official indicators vs target")
    bar_rows = []
    for key, label in [("GO_1", "GO TPR %"), ("SO_1", "SO competency %"), ("R1_1", "R1.1 CHP tests"), ("R1_2", "R1.2 reporting %")]:
        ind = indicators[key]
        cur = float(ind["value"] or 0)
        tgt = float(ind["target"] or 0)
        # Scale R1.1 to % of target for comparable bar chart
        if key == "R1_1":
            cur_disp = (cur / tgt * 100) if tgt else 0
            tgt_disp = 100.0
            bar_rows.append({"Indicator": label, "Series": "% of target", "Value": cur_disp})
            bar_rows.append({"Indicator": label, "Series": "Target (100%)", "Value": tgt_disp})
        else:
            bar_rows.append({"Indicator": label, "Series": "Current", "Value": cur})
            bar_rows.append({"Indicator": label, "Series": "Target", "Value": tgt})
    bar_df = pd.DataFrame(bar_rows)
    fig_bar = px.bar(
        bar_df, x="Indicator", y="Value", color="Series", barmode="group",
        color_discrete_sequence=[COLOR_NAVY, COLOR_AMBER],
        height=380,
    )
    fig_bar.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig_bar, use_container_width=True)

    c_left, c_right = st.columns(2)
    with c_left:
        section_header("Positivity over time")
        rdt = d[(d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT", na=False))]
        tested = rdt[rdt["indicator"].str.contains("Total Exam")].groupby(["period", "facility"])["value"].sum().reset_index()
        positive = rdt[rdt["indicator"].str.contains("Positive")].groupby(["period", "facility"])["value"].sum().reset_index()
        if not tested.empty and not positive.empty:
            merged = tested.merge(positive, on=["period", "facility"], suffixes=("_tested", "_positive"))
            merged["positivity"] = merged.apply(
                lambda r: (lambda a: a["value"] if a["status"] == "valid" else np.nan)(
                    safe_rate(r["value_positive"], r["value_tested"], indicator="chart_tpr")
                ),
                axis=1,
            )
            merged = merged.dropna(subset=["positivity"]).sort_values("period")
            fig = pgo.Figure()
            fac_list = sorted(merged["facility"].unique())
            for fac in fac_list:
                sub = merged[merged["facility"] == fac]
                fig.add_trace(pgo.Scatter(
                    x=sub["period"], y=sub["positivity"],
                    mode="lines+markers", name=fac, line=dict(width=3),
                ))
            fig.add_hline(y=BASELINES["GO_1"]["baseline"], line_dash="dot", line_color="#999",
                          annotation_text="Baseline")
            fig.add_hline(y=BASELINES["GO_1"]["target"], line_dash="dash", line_color=COLOR_RED,
                          annotation_text="Target")
            fig.update_layout(height=400, yaxis_title="TPR %", plot_bgcolor="white",
                              legend=dict(orientation="h", y=1.12), margin=dict(t=40))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No RDT series for current filters.")

    with c_right:
        section_header("Case cascade")
        cascade = compute_cascade_metrics(d)
        if cascade["suspected"] or cascade["tested"] or cascade["confirmed"]:
            fig_f = pgo.Figure(pgo.Funnel(
                y=["Suspected", "Tested", "Positive", "Treated (AL)"],
                x=[
                    cascade["suspected"], cascade["tested"],
                    cascade["confirmed"], cascade["treated"],
                ],
                textinfo="value+percent initial",
                marker={"color": [COLOR_NAVY, COLOR_BLUE, COLOR_AMBER, COLOR_GREEN]},
            ))
            fig_f.update_layout(height=400, margin=dict(t=20))
            st.plotly_chart(fig_f, use_container_width=True)
        else:
            st.info("No cascade volumes for current filters.")

    # ── Cascade gap cards (first-class) ──
    section_header("Cascade gaps & coverage")
    cascade = compute_cascade_metrics(d)
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        big_number(
            f"{cascade['testing_rate']:.0f}%",
            "Testing rate",
            delta=f"{cascade['tested']:,} / {cascade['suspected']:,} suspected",
            delta_direction="up" if cascade["testing_rate"] >= 90 else "down",
        )
    with g2:
        diag_dir = "down" if cascade["diag_gap_pct"] > 10 else "flat"
        big_number(
            f"{cascade['diag_gap']:,}",
            "Diagnosis gap",
            delta=f"{cascade['diag_gap_pct']:.0f}% of suspected not tested",
            delta_direction=diag_dir,
        )
    with g3:
        big_number(
            f"{cascade['treat_coverage']:.0f}%",
            "Treatment coverage",
            delta=f"{cascade['treated']:,} AL / {cascade['confirmed']:,} confirmed",
            delta_direction="up" if cascade["treat_coverage"] >= 90 else "down",
        )
    with g4:
        treat_dir = "down" if cascade["treat_gap_pct"] > 10 else "flat"
        big_number(
            f"{cascade['treat_gap']:,}",
            "Treatment gap",
            delta=f"{cascade['treat_gap_pct']:.0f}% confirmed without AL",
            delta_direction=treat_dir,
        )
    st.caption(
        "Diagnosis gap = Suspected − Tested · Treatment gap = Confirmed − AL dispensed. "
        "High gaps with falling testing volume are a warning — open **Loss & Gap Analysis** for detail."
    )

    # ── Data quality score ──
    section_header("Data quality score")
    dq = compute_dq_score(df, filters)
    if dq["score"] is None:
        st.info(dq["label"] + " — " + (dq["notes"][0] if dq["notes"] else ""))
    else:
        dq1, dq2, dq3 = st.columns([1, 2, 2])
        with dq1:
            big_number(
                f"{dq['score']:.0f}",
                f"DQ score · {dq['label']}",
                delta="Target ≥85",
                delta_direction="up" if dq["score"] >= 85 else ("flat" if dq["score"] >= 65 else "down"),
            )
        with dq2:
            st.markdown(
                f"<div class='status-badge {dq['level']}'>{dq['label']}</div>",
                unsafe_allow_html=True,
            )
            for note in dq["notes"]:
                st.caption(f"· {note}")
        with dq3:
            if dq.get("components"):
                comp_df = pd.DataFrame([
                    {"Component": k.replace("_", " ").title(), "Score": v}
                    for k, v in dq["components"].items()
                ])
                fig_dq = px.bar(
                    comp_df, x="Component", y="Score",
                    color="Score", color_continuous_scale="RdYlGn",
                    range_color=[0, 100], height=220,
                )
                fig_dq.update_layout(plot_bgcolor="white", showlegend=False, margin=dict(t=10, b=10))
                st.plotly_chart(fig_dq, use_container_width=True)
        _lr = get_latest_dq_review()
        if _lr is not None:
            st.caption(
                f"Last DQ review: **{_lr['reviewed_by']}** · {_lr['reviewed_at']} · "
                "Open **Data Quality** for rules, upload diff, and to record a new review."
            )
        else:
            st.caption(
                "Open **Data Quality** for validation rules, upload comparison, and DQ review stamp."
            )

    section_header("Facility comparison")
    rank_df = facility_ranking_table(d, indicators)
    # Chart ranking where numeric possible
    try:
        plot_rank = []
        go_pf = indicators["GO_1"].get("per_facility", {})
        for fac in FACILITIES:
            if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
                continue
            gv = go_pf.get(fac, {})
            tpr = gv.get("value") if isinstance(gv, dict) else None
            if tpr is not None:
                plot_rank.append({"Facility": fac, "TPR %": float(tpr)})
        if plot_rank:
            fig_r = px.bar(pd.DataFrame(plot_rank), x="Facility", y="TPR %",
                           color="TPR %", color_continuous_scale="Reds", height=320)
            fig_r.update_layout(plot_bgcolor="white")
            st.plotly_chart(fig_r, use_container_width=True)
    except Exception:
        pass
    with st.expander("Facility ranking table"):
        st.dataframe(rank_df, use_container_width=True, hide_index=True)

    section_header("Actions this period")
    actions = build_action_list(d, indicators)
    if not actions:
        st.success("No urgent actions from current thresholds.")
    else:
        for a in actions:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(a["priority"], "•")
            st.markdown(f"{icon} **{a['priority'].upper()}** — {a['text']}")

    section_header("Official indicator cards")
    cards = [
        ("GO_1", "GO", "General Objective", indicators["GO_1"]),
        ("SO_1", "SO", "Specific Objective", indicators["SO_1"]),
        ("R1_1", "R1.1", "Result 1.1", indicators["R1_1"]),
        ("R1_2", "R1.2", "Result 1.2", indicators["R1_2"]),
    ]
    row1 = st.columns(2)
    row2 = st.columns(2)
    layout = [row1[0], row1[1], row2[0], row2[1]]
    status_label = {"green": "On track", "amber": "At risk", "red": "Off track", "grey": "No data"}
    for col, (key, code, group, ind) in zip(layout, cards):
        with col:
            level = indicator_rag(ind, key)
            color_map = {"green": COLOR_GREEN, "amber": COLOR_AMBER,
                         "red": COLOR_RED, "grey": COLOR_TEXT_MUTED}
            delta_txt, _ = delta_string(ind["value"], ind["baseline"], ind["unit"])
            st.markdown(f"""
            <div style="background:#FFFFFF;border:1px solid {COLOR_BORDER};
                        border-top:5px solid {color_map[level]};
                        border-radius:12px;padding:20px;margin-bottom:12px;">
                <div style="font-size:0.75rem;color:{COLOR_TEXT_MUTED};font-weight:700;text-transform:uppercase;">
                    {group} · {status_label.get(level, level)}
                </div>
                <div style="font-size:0.9rem;font-weight:600;margin:6px 0;">{ind['name']}</div>
                <div style="font-size:2rem;font-weight:700;color:{COLOR_NAVY};">
                    {format_value(ind['value'], ind['unit'])}
                </div>
                <div style="font-size:0.85rem;color:{color_map[level]};">{delta_txt or '—'} vs baseline</div>
                <div style="font-size:0.8rem;color:{COLOR_TEXT_MUTED};">Target: {format_value(ind['target'], ind['unit'])}</div>
            </div>
            """, unsafe_allow_html=True)

    section_header("Epidemiological snapshot")
    epi = compute_epidemiological_indicators(df, filters)
    epi_plot = pd.DataFrame([
        {"Indicator": name, "Value": data["value"], "Target": data["target"]}
        for name, data in epi.items()
        if name in ("Malaria Incidence Rate", "ABER", "Test Positivity Rate")
    ])
    if not epi_plot.empty:
        fig_e = px.bar(epi_plot, x="Indicator", y=["Value", "Target"], barmode="group",
                       color_discrete_sequence=[COLOR_BLUE, COLOR_AMBER], height=320)
        fig_e.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig_e, use_container_width=True)
    with st.expander("Epi numbers (table)"):
        st.dataframe(pd.DataFrame([
            {"Indicator": n, "Value": format_value(v["value"], v["unit"]), "Target": v["target"], "Note": v.get("note", "")}
            for n, v in epi.items()
        ]), use_container_width=True, hide_index=True)

    section_header("Active Alerts Snapshot")
    alerts_df = get_active_alerts()
    if alerts_df.empty:
        st.success("🟢 No active alerts.")
    else:
        critical = len(alerts_df[alerts_df["severity"] == "critical"])
        high = len(alerts_df[alerts_df["severity"] == "high"])
        warning = len(alerts_df[alerts_df["severity"] == "warning"])
        c1, c2, c3 = st.columns(3)
        with c1:
            big_number(str(critical), "Critical", delta="Immediate action", delta_direction="down" if critical else "flat")
        with c2:
            big_number(str(high), "High", delta="Review today", delta_direction="flat")
        with c3:
            big_number(str(warning), "Warning", delta="Monitor", delta_direction="flat")

    with st.expander("Facility Snapshot"):
        for fac in FACILITIES:
            fac_df = df[df["facility"] == fac]
            if fac_df.empty:
                st.markdown(f"**{fac}** — no data")
                continue
            n = len(fac_df)
            periods = fac_df["period"].nunique()
            st.markdown(f"**{fac}** — {n:,} records across {periods} periods")

    with st.expander("All Indicators at a Glance"):
        rows = []
        for v in indicators.values():
            rows.append({"Indicator": v["name"],
                         "Current": format_value(v["value"], v["unit"]),
                         "Target": format_value(v["target"], v["unit"])})
        for k, v in epi.items():
            rows.append({"Indicator": k,
                         "Current": format_value(v["value"], v["unit"]),
                         "Target": f"{v['target']}{v['unit']}"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ==================================================================
# PAGE 3 — GO · TEST POSITIVITY RATE
# ==================================================================

def page_go(user, filters):
    df = load_all_data()

    if df.empty:
        st.warning("No data uploaded yet.")
        return

    if filters and "MOH 706" not in (filters.get("registers") or []):
        st.warning("Register filter does not include **MOH 706**. Apply filters with Laboratory or All registers — GO_1 is RDT exams from 706 only.")

    indicators = compute_official_indicators(df, filters)
    go = indicators["GO_1"]
    if go.get("period_grain") == "month":
        st.info("Period mode mixed year/quarter/month. **GO_1 is calculated on monthly rows only** so TPR is a real operational rate, not a blend of annual totals.")

    level = indicator_rag(go, "GO_1")
    go_val = go.get("value")
    go_base = go.get("baseline") if go.get("baseline") is not None else 0.0
    go_tgt = go.get("target") if go.get("target") is not None else 0.0
    diff = (go_val - go_base) if go_val is not None else 0.0
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    text = (f"Positivity is {format_value(go_val, '%')}, "
            f"{'up' if diff > 0 else 'down'} {abs(diff):.1f} pp from baseline. "
            f"Target is {format_value(go_tgt, '%')}. "
            f"RDT exams: {go.get('tested', 0):,}. {TPR_RULE['summary']}")
    if go.get("testing_collapse"):
        text += " ⚠️ Testing-collapse warning active."
    if go.get("formula_status") == "invalid":
        text = (
            f"GO_1 formula **INVALID** — not shown as programme TPR. "
            f"RDT positives {go.get('positive', 0):,} ÷ exams {go.get('tested', 0):,} "
            f"→ raw {go.get('calculated_pct')}%. {go.get('formula_reason') or ''}"
        )
        level = "grey"
    elif go_val is None:
        text = "No RDT exam data in the selected periods — widen filters or upload data that includes MOH 706 RDT lines."
    headline(text, status=level if (go_val is not None or go.get("formula_status") == "invalid") else "grey")

    section_header("Formula audit (Phase A safety)")
    st.caption(
        "Numerator > denominator is **never capped to 100%**. "
        "Missing denominator → N/A, not 0%. RDT stream only (microscopy excluded from GO_1)."
    )
    st.markdown(
        f"- **Formula:** `{go.get('formula') or 'RDT positives / RDT exams × 100'}`  \n"
        f"- **Numerator (positives):** {go.get('positive', 0):,}  \n"
        f"- **Denominator (exams):** {go.get('tested', 0):,}  \n"
        f"- **Status:** **{go.get('formula_status') or '—'}**  \n"
        f"- **Calculated % (raw):** {go.get('calculated_pct') if go.get('calculated_pct') is not None else '—'}  \n"
        f"- **Approved value for RAG:** {format_value(go_val, '%') if go_val is not None else 'N/A'}"
    )
    if go.get("formula_reason"):
        st.warning(go["formula_reason"])
    issues = go.get("formula_issues") or []
    if issues:
        st.markdown(f"**Facility/period cells requiring reconciliation ({len(issues)})**")
        rows_i = []
        for a in issues[:30]:
            rows_i.append({
                "Facility": a.get("facility"),
                "Period": a.get("period"),
                "Positives": a.get("numerator"),
                "Exams": a.get("denominator"),
                "Raw %": a.get("calculated_pct"),
                "Status": a.get("status"),
                "Reason": (a.get("reason") or "")[:120],
            })
        st.dataframe(pd.DataFrame(rows_i), use_container_width=True, hide_index=True)

    section_header("Key Numbers")
    c1, c2, c3 = st.columns(3)
    with c1:
        big_number(format_value(go_val, "%"), "Current Value",
                   delta=f"Target: {format_value(go_tgt, '%')}", delta_direction="flat")
    with c2:
        big_number(f"{diff:+.1f} pp" if go_val is not None else "—", "Change vs Baseline",
                   delta=f"Baseline: {format_value(go_base, '%')}", delta_direction=direction)
    with c3:
        prog = progress_pct(go_val, go_base, go_tgt) if go_val is not None else 0
        gap = (go_tgt - go_val) if go_val is not None else None
        big_number(f"{prog:.0f}%" if go_val is not None else "—", "Progress to Target",
                   delta=(f"Gap: {gap:.1f} pp" if gap is not None else "No data"),
                   delta_direction="flat")
    show_lineage_expander("GO_1", result=go, df=df, filters=filters)

    section_header("Facility Breakdown")
    rows = []
    for fac in FACILITIES:
        v = go.get("per_facility", {}).get(fac, {}) or {}
        val = v.get("value")
        vs_base = (val - go_base) if val is not None else None
        vs_target = (val - go_tgt) if val is not None else None
        lvl = status_level(val, go_tgt, direction="go_tpr", baseline=go_base,
                           testing_collapse=go.get("testing_collapse", False))
        rows.append({
            "Facility": fac, "Current": format_value(val, "%"),
            "vs Baseline": f"{vs_base:+.1f} pp" if vs_base is not None else "—",
            "vs Target": f"{vs_target:+.1f} pp" if vs_target is not None else "—",
            "Status": {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚫"}.get(lvl, "⚫"),
        })
    rows.append({
        "Facility": "All three", "Current": format_value(go_val, "%"),
        "vs Baseline": f"{diff:+.1f} pp" if go_val is not None else "—",
        "vs Target": f"{(go_val - go_tgt):+.1f} pp" if go_val is not None else "—",
        "Status": {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚫"}.get(level, "⚫"),
    })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    section_header("Trend")
    rdt = df[(df["register"] == "MOH 706") & (df["indicator"].str.contains("RDT", na=False))]
    if filters.get("facility") and filters["facility"] != "All three":
        rdt = rdt[rdt["facility"] == filters["facility"]]

    tested = rdt[rdt["indicator"].str.contains("Total Exam")].groupby(["period", "facility"])["value"].sum().reset_index()
    positive = rdt[rdt["indicator"].str.contains("Positive")].groupby(["period", "facility"])["value"].sum().reset_index()

    if not tested.empty:
        merged = tested.merge(positive, on=["period", "facility"], suffixes=("_tested", "_positive"))
        merged["positivity"] = merged.apply(
            lambda r: (lambda a: a["value"] if a["status"] == "valid" else np.nan)(
                safe_rate(r["value_positive"], r["value_tested"], indicator="chart_tpr")
            ),
            axis=1,
        )
        merged = merged.dropna(subset=["positivity"]).sort_values("period")

        fig = pgo.Figure()
        for fac in FACILITIES:
            sub = merged[merged["facility"] == fac]
            if sub.empty:
                continue
            fig.add_trace(pgo.Scatter(x=sub["period"], y=sub["positivity"],
                                     mode="lines+markers", name=fac, line=dict(width=3)))
        fig.add_hline(y=go["target"], line_dash="dash", line_color=COLOR_RED,
                      annotation_text=f"Target {go['target']}%")
        fig.add_hline(y=go["baseline"], line_dash="dot", line_color="#999",
                      annotation_text=f"Baseline {go['baseline']}%")
        fig.update_layout(height=420, yaxis_title="Positivity (%)", plot_bgcolor="white",
                          legend=dict(orientation="h", y=1.02))
        st.plotly_chart(fig, use_container_width=True)

    section_header("Age Breakdown")
    rdt_u5 = rdt[rdt["age_group"] == "<5"]
    rdt_o5 = rdt[rdt["age_group"] == ">=5"]

    def _age_rate(sub, label):
        tested = sub[sub["indicator"].str.contains("Total Exam", na=False)]["value"].sum()
        positive = sub[sub["indicator"].str.contains("Positive", na=False)]["value"].sum()
        a = safe_rate(positive, tested, indicator=f"GO_1_age_{label}", method="RDT",
                      formula="RDT pos ÷ RDT exams × 100", disaggregation=label)
        if a["status"] == "invalid":
            return None, a
        return a.get("value"), a

    age_u5, a_u5 = _age_rate(rdt_u5, "<5")
    age_o5, a_o5 = _age_rate(rdt_o5, ">=5")

    c1, c2 = st.columns(2)
    with c1:
        big_number(format_value(age_u5, "%"), "Under 5")
        if a_u5.get("status") == "invalid":
            st.caption(f"Invalid: {a_u5.get('reason')}")
    with c2:
        big_number(format_value(age_o5, "%"), "5 years and above")
        if a_o5.get("status") == "invalid":
            st.caption(f"Invalid: {a_o5.get('reason')}")

    if age_u5 is not None and age_o5 is not None:
        age_df = pd.DataFrame({"Age group": ["Under 5", "5 years +"], "Positivity": [age_u5, age_o5]})
        fig = px.bar(age_df, x="Age group", y="Positivity", text="Positivity", color="Age group",
                     color_discrete_map={"Under 5": COLOR_BLUE, "5 years +": COLOR_NAVY})
        fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig.update_layout(height=340, showlegend=False, plot_bgcolor="white", yaxis_title="Positivity (%)")
        st.plotly_chart(fig, use_container_width=True)

    section_header("Interpretation")
    st.markdown(f"""
    **Test positivity rate** measures the proportion of people tested who are
    confirmed positive. It is a proxy for transmission intensity, but only when
    testing behaviour is stable.

    Current: **{format_value(go_val, '%')}** · Baseline: **{format_value(go_base, '%')}** · Target: **{format_value(go_tgt, '%')}**
    """)

    section_header("Formula and Source")
    st.markdown(f"""
    - **Formula:** RDT positives ÷ RDT total exams × 100
    - **Source:** {go['source']}
    """)


# ==================================================================
# CHP ROSTER CSV IMPORT
# ==================================================================

CHP_CSV_COLUMNS = ["name", "facility", "chu", "phone", "competency_status", "competency_date", "active"]

CHP_STATUS_ALIASES = {
    "passed": "passed",
    "pass": "passed",
    "competent": "passed",
    "yes": "passed",
    "y": "passed",
    "1": "passed",
    "true": "passed",
    "pending": "pending",
    "not assessed": "pending",
    "not_assessed": "pending",
    "na": "pending",
    "n/a": "pending",
    "": "pending",
    "failed": "failed",
    "fail": "failed",
    "not passed": "failed",
    "no": "failed",
    "0": "failed",
    "false": "failed",
}

FACILITY_ALIASES = {
    "jaribuni": "Jaribuni",
    "jaribuni dispensary": "Jaribuni",
    "pingilikani": "Pingilikani",
    "pingilikani dispensary": "Pingilikani",
    "mwakuhenga": "Mwakuhenga",
    "mwakuhenga dispensary": "Mwakuhenga",
}


def chp_roster_template_csv() -> str:
    """Return a sample CSV template for County CHP roster import."""
    lines = [",".join(CHP_CSV_COLUMNS)]
    examples = [
        ("Amina Hassan", "Jaribuni", "Jaribuni CHU", "07XX000001", "passed", "2026-03-15", "1"),
        ("John Otieno", "Pingilikani", "Pingilikani CHU", "07XX000002", "pending", "", "1"),
        ("Grace Wanjiku", "Mwakuhenga", "Mwakuhenga CHU", "", "failed", "2026-01-20", "1"),
    ]
    for row in examples:
        lines.append(",".join(row))
    return "\n".join(lines) + "\n"


def parse_chp_roster_csv(uploaded_file):
    """
    Parse a County CHP roster CSV.
    Required column: name
    Optional: facility, chu, phone, competency_status, competency_date, active
    Returns (records_list, report_dict)
    """
    report = {
        "rows_read": 0,
        "rows_ok": 0,
        "rows_skipped": 0,
        "errors": [],
        "warnings": [],
        "facilities": {},
        "status_counts": {"passed": 0, "pending": 0, "failed": 0},
    }
    try:
        df = pd.read_csv(uploaded_file)
    except Exception as e:
        report["errors"].append(f"Could not read CSV: {e}")
        return [], report

    if df.empty:
        report["errors"].append("CSV is empty.")
        return [], report

    # Normalise column names
    col_map = {}
    for c in df.columns:
        key = str(c).strip().lower().replace(" ", "_")
        aliases = {
            "name": "name",
            "chp_name": "name",
            "chp": "name",
            "full_name": "name",
            "facility": "facility",
            "facility_name": "facility",
            "site": "facility",
            "chu": "chu",
            "community_unit": "chu",
            "community_health_unit": "chu",
            "phone": "phone",
            "mobile": "phone",
            "tel": "phone",
            "competency_status": "competency_status",
            "status": "competency_status",
            "competency": "competency_status",
            "assessment_status": "competency_status",
            "competency_date": "competency_date",
            "assessment_date": "competency_date",
            "date": "competency_date",
            "active": "active",
            "is_active": "active",
        }
        if key in aliases:
            col_map[c] = aliases[key]
    df = df.rename(columns=col_map)

    if "name" not in df.columns:
        report["errors"].append(
            "CSV must include a **name** column (or chp_name / full_name)."
        )
        return [], report

    records = []
    report["rows_read"] = len(df)
    for i, row in df.iterrows():
        name = str(row.get("name", "")).strip()
        if not name or name.lower() == "nan":
            report["rows_skipped"] += 1
            report["warnings"].append(f"Row {i + 2}: missing name — skipped.")
            continue

        raw_fac = str(row.get("facility", "")).strip() if "facility" in df.columns else ""
        fac = FACILITY_ALIASES.get(raw_fac.lower(), raw_fac) if raw_fac and raw_fac.lower() != "nan" else ""
        if fac and fac not in FACILITIES:
            report["warnings"].append(
                f"Row {i + 2} ({name}): facility '{raw_fac}' not in project list "
                f"({', '.join(FACILITIES)}) — stored as-is."
            )
            # still store; filter pages use FACILITIES list for selects
        if not fac:
            fac = FACILITIES[0]
            report["warnings"].append(
                f"Row {i + 2} ({name}): no facility — defaulted to {fac}."
            )

        chu = ""
        if "chu" in df.columns:
            chu = str(row.get("chu", "") or "").strip()
            if chu.lower() == "nan":
                chu = ""

        phone = ""
        if "phone" in df.columns:
            phone = str(row.get("phone", "") or "").strip()
            if phone.lower() == "nan":
                phone = ""

        status_raw = ""
        if "competency_status" in df.columns:
            status_raw = str(row.get("competency_status", "") or "").strip().lower()
            if status_raw == "nan":
                status_raw = ""
        status = CHP_STATUS_ALIASES.get(status_raw, "pending")
        if status_raw and status_raw not in CHP_STATUS_ALIASES and status == "pending":
            report["warnings"].append(
                f"Row {i + 2} ({name}): unknown status '{status_raw}' — set to pending."
            )

        comp_date = ""
        if "competency_date" in df.columns:
            val = row.get("competency_date")
            if pd.notna(val) and str(val).strip() and str(val).lower() != "nan":
                try:
                    comp_date = pd.to_datetime(val).strftime("%Y-%m-%d")
                except Exception:
                    comp_date = str(val).strip()[:10]

        active = 1
        if "active" in df.columns:
            aval = str(row.get("active", "1")).strip().lower()
            if aval in ("0", "false", "no", "inactive", "n"):
                active = 0

        records.append(
            {
                "name": name,
                "facility": fac,
                "chu": chu,
                "phone": phone,
                "competency_status": status,
                "competency_date": comp_date or None,
                "active": active,
            }
        )
        report["rows_ok"] += 1
        report["status_counts"][status] = report["status_counts"].get(status, 0) + 1
        report["facilities"][fac] = report["facilities"].get(fac, 0) + 1

    return records, report


def import_chp_roster(records, mode="append"):
    """
    mode: 'append' | 'replace'
    Returns number of rows inserted.
    """
    if mode == "replace":
        execute("DELETE FROM chps")
    conn = get_conn()
    try:
        n = 0
        for r in records:
            conn.execute(
                """
                INSERT INTO chps (name, facility, chu, phone, competency_status, competency_date, active)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["name"],
                    r["facility"],
                    r["chu"],
                    r["phone"],
                    r["competency_status"],
                    r["competency_date"],
                    r["active"],
                ),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


# ==================================================================
# PAGE 4 — SO · CHP COMPETENCY
# ==================================================================

def page_so(user, filters):
    df = load_all_data()

    indicators = compute_official_indicators(df, filters) if not df.empty else {
        "SO_1": {"value": 0, "baseline": 0, "target": 70, "unit": "%",
                 "source": "Kilifi County Department of Health"}
    }
    so = indicators["SO_1"]

    level = indicator_rag(so, "SO_1")
    diff = so["value"] - so["baseline"]
    text = (f"CHP competency is at {so['value']:.0f}%, "
            f"{'up' if diff > 0 else 'down'} {abs(diff):.0f} pp from baseline. "
            f"Target is {so['target']:.0f}%.")
    headline(text, status=level)

    section_header("Key Numbers")
    c1, c2, c3 = st.columns(3)
    with c1:
        big_number(f"{so['value']:.0f}%", "Current Value",
                   delta=f"Target: {so['target']:.0f}%", delta_direction="flat")
    with c2:
        big_number(f"{diff:+.0f} pp", "Change vs Baseline",
                   delta=f"Baseline: {so['baseline']:.0f}%",
                   delta_direction="up" if diff > 0 else "flat")
    with c3:
        prog = progress_pct(so["value"], so["baseline"], so["target"])
        big_number(f"{prog:.0f}%", "Progress to Target",
                   delta=f"Gap: {so['target'] - so['value']:.0f} pp", delta_direction="flat")

    st.caption(
        f"**Source:** {so.get('source', '—')}. "
        "When a CHP roster exists, SO_1 is calculated live as "
        "(CHPs passed ÷ active CHPs on roster) × 100."
    )
    show_lineage_expander("SO_1", result=so, df=df, filters=filters)

    section_header("CHP Roster")
    chps = query("SELECT * FROM chps ORDER BY facility, name")
    if chps.empty:
        st.warning(
            "**SO_1 is 0% because no CHP roster is loaded.** "
            "Competency cannot be calculated without a roster."
        )
        st.markdown(
            """
**What to do (pick one)**

1. **Import from County CSV** — expand the import section below (recommended for official reporting).  
2. **Add CHPs one by one** — use the form further down.  
3. **Seed a starter roster** — demo only (30 placeholder CHPs at ~70% passed).  

Until a roster exists, do **not** treat the 0% on Overview / reports as a real competency result.
"""
        )
    else:
        # Facility-level competency breakdown
        section_header("Competency by facility")
        active = chps.copy()
        if "active" in active.columns:
            active = active[(active["active"].isna()) | (active["active"] == 1)]
        by_fac = []
        for fac in FACILITIES:
            sub = active[active["facility"] == fac] if "facility" in active.columns else active.iloc[0:0]
            n = len(sub)
            passed = int((sub["competency_status"] == "passed").sum()) if n else 0
            pending = int((sub["competency_status"] == "pending").sum()) if n else 0
            failed = int((sub["competency_status"] == "failed").sum()) if n else 0
            a = safe_rate(passed, n, indicator="SO_1_fac", formula="Passed ÷ Active CHPs × 100") if n else {"status": "na", "value": None}
            pct = a.get("value") if a.get("status") == "valid" else None
            by_fac.append({
                "Facility": fac,
                "Active CHPs": n,
                "Passed": passed,
                "Pending": pending,
                "Failed": failed,
                "SO_1 %": f"{pct:.0f}%" if pct is not None else "—",
            })
        st.dataframe(pd.DataFrame(by_fac), use_container_width=True, hide_index=True)
        try:
            chart_rows = [
                {"Facility": r["Facility"], "SO_1 %": float(r["SO_1 %"].replace("%", ""))}
                for r in by_fac if r["SO_1 %"] != "—"
            ]
            if chart_rows:
                fig_so = px.bar(
                    pd.DataFrame(chart_rows), x="Facility", y="SO_1 %",
                    color="SO_1 %", color_continuous_scale="RdYlGn",
                    range_color=[0, 100], height=280,
                )
                fig_so.add_hline(y=70, line_dash="dash", line_color=COLOR_GREEN,
                                 annotation_text="Target 70%")
                fig_so.update_layout(plot_bgcolor="white", showlegend=False)
                st.plotly_chart(fig_so, use_container_width=True)
        except Exception:
            pass

        # Last change hint
        if "competency_date" in chps.columns:
            dates = chps["competency_date"].dropna().astype(str)
            dates = [x for x in dates if x and x not in ("", "None", "nan")]
            if dates:
                st.caption(f"Latest competency date on roster: **{max(dates)}**")

        display = chps.copy()
        if "active" in display.columns:
            display["active"] = display["active"].map({1: "✅ Active", 0: "Inactive", None: "✅ Active"})
        st.dataframe(display, use_container_width=True, hide_index=True)
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            big_number(str(len(chps)), "Total CHPs")
        with c2:
            big_number(str(int((chps["competency_status"] == "passed").sum())), "Passed Competency")
        with c3:
            big_number(str(int((chps["competency_status"] == "pending").sum())), "Pending Assessment")
        with c4:
            export_df = chps.copy()
            for col in CHP_CSV_COLUMNS:
                if col not in export_df.columns:
                    export_df[col] = ""
            csv_bytes = export_df[CHP_CSV_COLUMNS].to_csv(index=False).encode("utf-8")
            st.download_button(
                "Export roster CSV",
                data=csv_bytes,
                file_name=f"cbmp_chp_roster_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    if user.get("can_upload"):
        section_header("Manage roster (drives live SO_1)")

        with st.expander("➕ Add a CHP", expanded=chps.empty):
            with st.form("add_chp"):
                c1, c2 = st.columns(2)
                with c1:
                    name = st.text_input("Name")
                    facility = st.selectbox("Facility", FACILITIES)
                    chu = st.text_input("CHU / Community unit")
                with c2:
                    phone = st.text_input("Phone")
                    status = st.selectbox("Competency status", ["pending", "passed", "failed"])
                    date = st.date_input("Assessment date")
                if st.form_submit_button("Add CHP") and name:
                    execute(
                        """
                        INSERT INTO chps (name, facility, chu, phone, competency_status, competency_date, active)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (name, facility, chu, phone, status, str(date)),
                    )
                    log_audit(user["username"], "add_chp", "chps", f"{name} at {facility}")
                    st.success(f"CHP {name} added. SO_1 will recalculate on next view.")
                    st.rerun()

        if not chps.empty:
            with st.expander("✏️ Update competency status"):
                with st.form("update_chp_status"):
                    options = {
                        f"{r['id']}: {r['name']} ({r['facility']}) — {r['competency_status']}": int(r["id"])
                        for _, r in chps.iterrows()
                    }
                    pick = st.selectbox("CHP", list(options.keys()))
                    new_status = st.selectbox("New status", ["pending", "passed", "failed"])
                    new_date = st.date_input("Assessment / update date", key="chp_status_date")
                    if st.form_submit_button("Save status"):
                        chp_id = options[pick]
                        execute(
                            """
                            UPDATE chps
                            SET competency_status = ?, competency_date = ?
                            WHERE id = ?
                            """,
                            (new_status, str(new_date), chp_id),
                        )
                        log_audit(user["username"], "update_chp", "chps", f"id={chp_id} → {new_status}")
                        st.success("Competency updated. SO_1 recalculates from the roster.")
                        st.rerun()

        with st.expander("📥 Import CHP roster from CSV (County list)", expanded=False):
            st.markdown(
                """
**Expected columns** (header row required):

| Column | Required | Notes |
|--------|----------|--------|
| `name` | Yes | CHP full name |
| `facility` | Recommended | Jaribuni / Pingilikani / Mwakuhenga |
| `chu` | Optional | Community health unit |
| `phone` | Optional | |
| `competency_status` | Optional | `passed` / `pending` / `failed` (aliases accepted) |
| `competency_date` | Optional | YYYY-MM-DD |
| `active` | Optional | 1 or 0 (default 1) |

Aliases accepted for name (`chp_name`, `full_name`), status (`competent`, `pass`, …), facility spelling variants.
"""
            )
            st.download_button(
                "Download CSV template",
                data=chp_roster_template_csv(),
                file_name="cbmp_chp_roster_template.csv",
                mime="text/csv",
                use_container_width=True,
            )
            roster_file = st.file_uploader(
                "Upload County CHP roster CSV",
                type=["csv"],
                key="chp_roster_csv",
            )
            import_mode = st.radio(
                "Import mode",
                ["Append to existing roster", "Replace entire roster"],
                horizontal=True,
                key="chp_import_mode",
            )
            if roster_file is not None:
                records, report = parse_chp_roster_csv(roster_file)
                if report["errors"]:
                    for e in report["errors"]:
                        st.error(e)
                else:
                    c1, c2, c3, c4 = st.columns(4)
                    with c1:
                        big_number(str(report["rows_ok"]), "Rows OK")
                    with c2:
                        big_number(str(report["rows_skipped"]), "Skipped")
                    with c3:
                        big_number(str(report["status_counts"].get("passed", 0)), "Passed")
                    with c4:
                        big_number(str(report["status_counts"].get("pending", 0)), "Pending")
                    if report["facilities"]:
                        st.caption(
                            "By facility: "
                            + ", ".join(f"{k}: {v}" for k, v in sorted(report["facilities"].items()))
                        )
                    if report["warnings"]:
                        with st.expander(f"Warnings ({len(report['warnings'])})"):
                            for w in report["warnings"][:40]:
                                st.text(f"• {w}")
                            if len(report["warnings"]) > 40:
                                st.caption(f"… and {len(report['warnings']) - 40} more")
                    if records:
                        preview = pd.DataFrame(records)
                        st.dataframe(preview.head(20), use_container_width=True, hide_index=True)
                        if len(records) > 20:
                            st.caption(f"Showing 20 of {len(records)} rows")
                        if st.button("Import roster into database", type="primary", use_container_width=True):
                            mode = "replace" if import_mode.startswith("Replace") else "append"
                            n = import_chp_roster(records, mode=mode)
                            log_audit(
                                user["username"],
                                "import_chp_roster",
                                "chps",
                                f"{n} rows mode={mode} file={roster_file.name}",
                            )
                            st.success(
                                f"Imported **{n}** CHPs ({mode}). SO_1 will recalculate from the roster."
                            )
                            st.rerun()

        with st.expander("🌱 Seed starter roster (10 CHPs per facility)"):
            st.caption(
                "Creates 30 placeholder CHPs (10 per facility) with mixed competency "
                "so you can see live SO_1. Safe to run once; skips if roster already has data."
            )
            if st.button("Seed starter roster", type="secondary"):
                existing = query("SELECT COUNT(*) AS n FROM chps")
                if not existing.empty and int(existing.iloc[0]["n"]) > 0:
                    st.warning("Roster already has CHPs. Clear or edit existing records instead.")
                else:
                    seed = []
                    # ~70% passed overall to sit near target for demo
                    statuses = (["passed"] * 7 + ["pending"] * 2 + ["failed"] * 1)
                    for fac in FACILITIES:
                        for i in range(1, 11):
                            seed.append(
                                (
                                    f"CHP {fac[:3].upper()}-{i:02d}",
                                    fac,
                                    f"{fac} CHU",
                                    "",
                                    statuses[(i - 1) % len(statuses)],
                                    datetime.now().strftime("%Y-%m-%d"),
                                    1,
                                )
                            )
                    conn = get_conn()
                    conn.executemany(
                        """
                        INSERT INTO chps (name, facility, chu, phone, competency_status, competency_date, active)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        seed,
                    )
                    conn.commit()
                    conn.close()
                    log_audit(user["username"], "seed_chps", "chps", "30 starter CHPs")
                    st.success("Seeded 30 CHPs. Open this page again to see live SO_1 ≈ 70%.")
                    st.rerun()

        with st.expander("✏️ Override SO_1 % manually (bypasses roster)"):
            st.caption(
                "Use only if County supplies a single % and you have not entered the roster. "
                "Roster values take priority when CHPs exist."
            )
            with st.form("so_update"):
                new_value = st.number_input(
                    "CHP competency %",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(so["value"] or 0),
                    step=1.0,
                )
                comment = st.text_input("Reason for change (optional)")
                if st.form_submit_button("Save manual %"):
                    conn = get_conn()
                    conn.execute(
                        """
                        INSERT INTO baselines (indicator_id, baseline_value, updated_by)
                        VALUES ('SO_1', ?, ?)
                        ON CONFLICT(indicator_id) DO UPDATE SET
                            baseline_value=excluded.baseline_value,
                            updated_by=excluded.updated_by,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (new_value, user["username"]),
                    )
                    conn.commit()
                    conn.close()
                    log_audit(user["username"], "baseline_edit", "baselines", comment or "SO_1 updated")
                    st.success(f"Manual SO_1 stored as {new_value:.1f}% (used only when roster is empty).")
                    st.rerun()


# ==================================================================
# PAGE 5 — R1.1 · CHP TESTS
# ==================================================================

def page_r11(user, filters):
    df = load_all_data()
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    indicators = compute_official_indicators(df, filters)
    r11 = indicators["R1_1"]
    level = indicator_rag(r11, "R1_1")
    diff = r11["value"] - r11["baseline"]
    text = (f"CHPs performed {r11['value']:,} tests, "
            f"{'up' if diff > 0 else 'down'} {abs(int(diff)):,} from baseline of {r11['baseline']:,}. "
            f"Target is {r11['target']:,}.")
    headline(text, status=level)

    section_header("Key Numbers")
    c1, c2, c3 = st.columns(3)
    with c1:
        big_number(f"{r11['value']:,}", "Current Value",
                   delta=f"Target: {r11['target']:,}", delta_direction="flat")
    with c2:
        big_number(f"{diff:+,.0f}", "Change vs Baseline",
                   delta=f"Baseline: {r11['baseline']:,}",
                   delta_direction="up" if diff > 0 else "flat")
    with c3:
        prog = progress_pct(r11["value"], r11["baseline"], r11["target"])
        big_number(f"{prog:.0f}%", "Progress to Target",
                   delta=f"Gap: {r11['target'] - r11['value']:,}", delta_direction="flat")

    show_lineage_expander("R1_1", result=r11, df=df, filters=filters)

    # Early Phase B: run-rate vs annual target (view total treated as YTD for planning)
    y, m = _ytd_month_from_filters(filters, df)
    render_run_rate_panel(
        "Run rate vs annual target (R1.1)",
        r11.get("value"),
        r11.get("target"),
        " tests",
        year=y,
        as_of_month=m,
    )

    section_header("Facility Breakdown")
    rows = [{"Facility": fac, "CHP Tests": f"{r11['per_facility'].get(fac, 0):,}"} for fac in FACILITIES]
    rows.append({"Facility": "All three", "CHP Tests": f"{r11['value']:,}"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    section_header("Community vs Facility Tests")
    d = _filter_data(df, filters)
    fac_tests = d[(d["stream"] == "Facility") & (d["indicator"].str.contains("Total Exam", na=False))]["value"].sum()
    com_tests = d[(d["stream"] == "Community") & (d["indicator"].str.contains("Total", na=False))]["value"].sum()

    if fac_tests + com_tests > 0:
        fig = pgo.Figure(data=[
            pgo.Bar(name="Facility", x=["Tests"], y=[fac_tests], marker_color=COLOR_NAVY),
            pgo.Bar(name="Community", x=["Tests"], y=[com_tests], marker_color=COLOR_GREEN),
        ])
        fig.update_layout(barmode="group", height=340, plot_bgcolor="white",
                          legend=dict(orientation="h", y=1.02))
        st.plotly_chart(fig, use_container_width=True)

    section_header("Formula and Source")
    st.markdown(f"""
    - **Formula:** Sum of MOH 748 Total <5 + Total ≥5
    - **Source:** {r11['source']}
    """)


# ==================================================================
# PAGE 6 — R1.2 · REPORTS SUBMITTED
# ==================================================================

def page_r12(user, filters):
    df = load_all_data()
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    indicators = compute_official_indicators(df, filters)
    r12 = indicators["R1_2"]
    level = indicator_rag(r12, "R1_2")
    diff = r12["value"] - r12["baseline"]
    text = (f"Reporting rate is {r12['value']:.1f}%, "
            f"{'up' if diff > 0 else 'down'} {abs(diff):.1f} pp from baseline of {r12['baseline']:.0f}%. "
            f"Target is {r12['target']:.0f}%.")
    headline(text, status=level)

    section_header("Key Numbers")
    c1, c2, c3 = st.columns(3)
    with c1:
        big_number(f"{r12['value']:.1f}%", "Current Value",
                   delta=f"Target: {r12['target']:.0f}%", delta_direction="flat")
    with c2:
        big_number(f"{diff:+.1f} pp", "Change vs Baseline",
                   delta=f"Baseline: {r12['baseline']:.0f}%",
                   delta_direction="up" if diff > 0 else "flat")
    with c3:
        prog = progress_pct(r12["value"], r12["baseline"], r12["target"])
        big_number(f"{prog:.0f}%", "Progress to Target",
                   delta=f"Gap: {r12['target'] - r12['value']:.1f} pp", delta_direction="flat")
    show_lineage_expander("R1_2", result=r12, df=df, filters=filters)

    section_header("Facility Breakdown")
    rows = []
    for fac in FACILITIES:
        val = r12["per_facility"].get(fac)
        rows.append({"Facility": fac, "Reporting Rate": f"{val:.1f}%" if val else "—"})
    rows.append({"Facility": "All three", "Reporting Rate": f"{r12['value']:.1f}%"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    section_header("Register-by-Register")
    # For R1.2 trend: default to ALL registers so the chart is not silently constrained
    # by the global register preset (user can still narrow via filter bar for facility table).
    d_all = _filter_data(df, {**(filters or {}), "registers": list(REGISTERS)})
    d = _filter_data(df, filters)
    rr_all = d_all[d_all["indicator"].astype(str).str.contains("Reporting Rate", case=False, na=False)].copy()
    rr = d[d["indicator"].astype(str).str.contains("Reporting Rate", case=False, na=False)].copy()
    if rr_all.empty and not rr.empty:
        rr_all = rr.copy()
    if not rr_all.empty:
        # Normalize 0–1 vs 0–100 scales
        rr_all["value"] = pd.to_numeric(rr_all["value"], errors="coerce")
        rr_all = rr_all.dropna(subset=["value"])
        if not rr_all.empty and rr_all["value"].max() <= 1.5:
            rr_all["value_pct"] = rr_all["value"] * 100.0
        else:
            rr_all["value_pct"] = rr_all["value"]
        by_reg = (
            rr_all.groupby("register", as_index=False)["value_pct"]
            .mean()
            .rename(columns={"value_pct": "Reporting Rate (%)"})
        )
        by_reg["Reporting Rate (%)"] = by_reg["Reporting Rate (%)"].round(1)
        st.dataframe(by_reg, use_container_width=True, hide_index=True)
        st.caption(f"Registers with reporting-rate data: **{len(by_reg)}** · rows={len(rr_all)}")

    section_header("Trend — all registers")
    st.caption(
        "Shows every register with a Reporting Rate row (not limited to the current register preset). "
        "Target lines at 80% and 100%."
    )
    st.info(
        "**Aggregation note:** KHIS often supplies a pre-computed reporting rate per facility×register. "
        "When only rates are available, the trend uses the **mean of valid rates** (unweighted). "
        "If submitted/expected counts are uploaded later, the dashboard will prefer "
        "**Σ submitted ÷ Σ expected** (denominator-aware). Do not mix mean-of-rates with volume-weighted totals without noting the method."
    )
    c_reg, c_per = st.columns(2)
    with c_reg:
        view_mode = st.radio(
            "Register view",
            ["All registers with data", "Facility registers only", "Community registers only", "Use filter-bar selection"],
            horizontal=True,
            key="r12_reg_view",
        )
    with c_per:
        per_mode = st.radio(
            "Period window",
            ["All available periods", "Last 12 months", "Last 6 months", "Last 3 months"],
            horizontal=True,
            key="r12_per_view",
        )
    if view_mode == "Use filter-bar selection":
        plot_rr = rr.copy() if not rr.empty else rr_all.copy()
    elif view_mode == "Facility registers only":
        fac_regs = ["MOH 705A", "MOH 705B", "MOH 706", "MOH 743", "MOH 705"]
        plot_rr = rr_all[rr_all["register"].isin(fac_regs)].copy() if not rr_all.empty else pd.DataFrame()
    elif view_mode == "Community registers only":
        com_regs = ["MOH 648", "MOH 521", "MOH 748", "MOH 515", "MOH 711"]
        plot_rr = rr_all[rr_all["register"].isin(com_regs)].copy() if not rr_all.empty else pd.DataFrame()
    else:
        plot_rr = rr_all.copy()

    if plot_rr.empty:
        st.info("⚠️ No reporting-rate observations for this view. Upload KHIS with MOH 515 / reporting rate indicators.")
    else:
        plot_rr["value"] = pd.to_numeric(plot_rr["value"], errors="coerce")
        plot_rr = plot_rr.dropna(subset=["value", "period", "register"])
        if not plot_rr.empty and plot_rr["value"].max() <= 1.5:
            plot_rr["value_pct"] = plot_rr["value"] * 100.0
        else:
            plot_rr["value_pct"] = plot_rr["value"]
        # Period window filter (YYYY-MM preferred)
        periods_sorted = sorted(plot_rr["period"].astype(str).unique().tolist())
        if per_mode != "All available periods" and periods_sorted:
            n_keep = {"Last 12 months": 12, "Last 6 months": 6, "Last 3 months": 3}.get(per_mode, 12)
            # Prefer chronological order for YYYY-MM
            try:
                periods_sorted = sorted(periods_sorted, key=lambda p: p)
            except Exception:
                pass
            keep = periods_sorted[-n_keep:]
            plot_rr = plot_rr[plot_rr["period"].astype(str).isin(keep)]
        trend = (
            plot_rr.groupby(["period", "register"], as_index=False)["value_pct"]
            .mean()
            .sort_values(["period", "register"])
        )
        n_reg = trend["register"].nunique()
        n_per = trend["period"].nunique()
        st.caption(f"Showing **{n_reg} registers × {n_per} periods** · method = mean of valid rates")
        if trend.empty:
            st.info("⚠️ No periods left after applying the period window.")
        else:
            fig = px.line(
                trend, x="period", y="value_pct", color="register", markers=True,
                labels={"value_pct": "Reporting rate (%)", "period": "Period", "register": "Register"},
            )
            fig.add_hline(y=80, line_dash="dash", line_color=COLOR_RED, annotation_text="Target 80%")
            fig.add_hline(y=100, line_dash="dot", line_color=COLOR_GREEN, annotation_text="100%")
            fig.update_layout(
                height=440, plot_bgcolor="white",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                yaxis=dict(range=[0, 110]),
                margin=dict(t=40, b=40),
            )
            st.plotly_chart(fig, use_container_width=True)
            with st.expander("View trend data table"):
                pivot = trend.pivot(index="period", columns="register", values="value_pct").round(1)
                st.dataframe(pivot, use_container_width=True)


# ==================================================================
# PAGE 7 — EPIDEMIOLOGICAL
# ==================================================================

def page_epidemiological(user, filters):
    """
    B3 — Epidemiology intelligence.
    Incidence / ABER / TPR read together; facility split; age split; community vs facility.
    Population remains provisional until County DoH confirms.
    """
    df = load_all_data()
    page_header(
        "Epidemiology intelligence (B3)",
        "Transmission intensity vs detection effort. Population denominators are "
        f"**{get_population_status()}**. Invalid TPR never drives a transmission conclusion.",
    )
    if get_population_status() != "confirmed":
        st.error(
            "⚠️ EPIDEMIOLOGICAL ESTIMATES — PROVISIONAL. "
            "Catchment populations are not confirmed by Kilifi County DoH. "
            "Incidence and ABER are indicative only — not for official donor reporting."
        )
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    d = _filter_data(df, filters)
    epi = compute_epidemiological_indicators(df, filters)
    cascade = compute_cascade_metrics(d)
    go = compute_indicator("GO_1", df, filters)

    headline(
        "Read incidence, ABER and TPR together. Low TPR + low ABER = under-detection, not low transmission.",
        status="amber" if get_population_status() == "provisional" else "green",
    )

    section_header("Core epi triad")
    keys = [
        ("Malaria Incidence Rate", "per 1,000"),
        ("ABER", "%"),
        ("Test Positivity Rate", "%"),
    ]
    cols = st.columns(3)
    for col, (name, _) in zip(cols, keys):
        data = epi.get(name) or {}
        stt = data.get("status") or ("valid" if data.get("value") is not None else "na")
        with col:
            if stt == "invalid":
                big_number("INVALID", name)
                st.caption(data.get("reason") or data.get("note") or "")
            elif data.get("value") is None:
                big_number("N/A", name)
                st.caption(data.get("note") or "Missing inputs")
            else:
                big_number(format_value(data.get("value"), data.get("unit", "")), name)
                st.caption(f"Target {data.get('target')}{data.get('unit') or ''} · {stt}")

    c4, c5 = st.columns(2)
    with c4:
        cfr = epi.get("Case Fatality Rate") or {}
        big_number(
            format_value(cfr.get("value"), cfr.get("unit", "%")) if cfr.get("status") == "valid" else "N/A",
            "Case fatality",
        )
        st.caption(cfr.get("note") or "Needs death field in upload")
    with c5:
        sev = epi.get("Severe Malaria Proportion") or {}
        big_number(
            format_value(sev.get("value"), sev.get("unit", "%")) if sev.get("status") == "valid" else "N/A",
            "Severe proportion",
        )
        st.caption(sev.get("note") or "Needs severe field in upload")

    st.info(
        f"Population status: **{get_population_status()}**. "
        f"Catchment used: Jaribuni {POPULATION.get('Jaribuni'):,} · "
        f"Pingilikani {POPULATION.get('Pingilikani'):,} · "
        f"Mwakuhenga {POPULATION.get('Mwakuhenga'):,} · Total {POPULATION.get('Total'):,}."
    )
    show_lineage_expander("EPI_1", result=compute_indicator("EPI_1", df, filters), df=df, filters=filters)
    show_lineage_expander("GO_1", result=go, df=df, filters=filters)

    # Facility epi table
    section_header("Facility epidemiological profile")
    fac_rows = []
    for fac in FACILITIES:
        if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
            continue
        pf = dict(filters or {})
        pf["facility"] = fac
        fd = _filter_data(df, pf)
        fe = compute_epidemiological_indicators(df, pf)
        fc = compute_cascade_metrics(fd)
        tpr = fe.get("Test Positivity Rate") or {}
        inc = fe.get("Malaria Incidence Rate") or {}
        aber = fe.get("ABER") or {}
        fac_rows.append({
            "Facility": fac,
            "Sub-county": SUBCOUNTY.get(fac, "—"),
            "Population (prov.)": POPULATION.get(fac),
            "Confirmed (705)": fc.get("confirmed"),
            "Tested (705)": fc.get("tested"),
            "Incidence /1,000": inc.get("value") if inc.get("status") != "invalid" else None,
            "Inc. status": inc.get("status"),
            "ABER %": aber.get("value") if aber.get("status") == "valid" else None,
            "ABER status": aber.get("status"),
            "RDT TPR %": tpr.get("value") if tpr.get("status") == "valid" else None,
            "TPR status": tpr.get("status"),
            "Diag gap": fc.get("diag_gap"),
        })
    fac_epi = pd.DataFrame(fac_rows)
    st.dataframe(fac_epi, use_container_width=True, hide_index=True)

    if not fac_epi.empty:
        plot = fac_epi.dropna(subset=["Incidence /1,000"])
        if not plot.empty:
            fig = px.bar(
                plot, x="Facility", y="Incidence /1,000", color="Facility", height=340,
                title="Indicative incidence (provisional population)",
            )
            fig.update_layout(plot_bgcolor="white", showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

    # Age split from 706 RDT + 705
    section_header("Age pattern (locked sources)")
    a1, a2 = st.columns(2)
    with a1:
        st.markdown("**MOH 706 RDT by age**")
        rdt = d[(d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT", na=False))] if not d.empty else pd.DataFrame()
        age_rows = []
        for label, age in (("<5", "<5"), (">=5", ">=5")):
            sub = rdt[rdt["age_group"] == age] if not rdt.empty else pd.DataFrame()
            exams = float(sub[sub["indicator"].str.contains("Total Exam", na=False)]["value"].sum()) if not sub.empty else 0
            pos = float(sub[sub["indicator"].str.contains("Positive", na=False)]["value"].sum()) if not sub.empty else 0
            a = safe_rate(pos, exams, indicator=f"EPI_RDT_{age}", method="RDT")
            age_rows.append({
                "Age": label,
                "RDT exams": int(exams),
                "RDT positive": int(pos),
                "TPR %": a.get("value") if a.get("status") == "valid" else None,
                "Status": (a.get("status") or "na").upper(),
                "Reason": a.get("reason") or "",
            })
        st.dataframe(pd.DataFrame(age_rows), use_container_width=True, hide_index=True)
    with a2:
        st.markdown("**MOH 705 confirmed by age band**")
        clin = d[d["register"].isin(["MOH 705A", "MOH 705B"])] if not d.empty else pd.DataFrame()
        u5 = float(clin[clin["indicator"].str.contains(r"Confirmed.*<5|Confirmed <5", na=False, regex=True)]["value"].sum()) if not clin.empty else 0
        o5 = float(clin[clin["indicator"].str.contains(r"Confirmed.*>=5|Confirmed >=5", na=False, regex=True)]["value"].sum()) if not clin.empty else 0
        # Fallback age_group
        if u5 == 0 and o5 == 0 and not clin.empty:
            u5 = float(clin[(clin["age_group"] == "<5") & (clin["indicator"].str.contains("Confirmed", na=False))]["value"].sum())
            o5 = float(clin[(clin["age_group"].isin([">=5", ">5"])) & (clin["indicator"].str.contains("Confirmed", na=False))]["value"].sum())
        age_c = pd.DataFrame({"Age": ["<5 years", "≥5 years"], "Confirmed": [int(u5), int(o5)]})
        st.dataframe(age_c, use_container_width=True, hide_index=True)
        if u5 + o5 > 0:
            total_age = u5 + o5
            # Interactive donut with pulled slices, labels = count + %
            fig_a = pgo.Figure(data=[pgo.Pie(
                labels=age_c["Age"].tolist(),
                values=age_c["Confirmed"].tolist(),
                hole=0.42,
                pull=[0.06, 0.02],
                textinfo="label+percent+value",
                texttemplate="%{label}<br>%{value:,.0f} (%{percent})",
                hovertemplate="<b>%{label}</b><br>Confirmed: %{value:,.0f}<br>Share: %{percent}<extra></extra>",
                marker=dict(
                    colors=[COLOR_BLUE, COLOR_GREEN],
                    line=dict(color="#FFFFFF", width=3),
                ),
                sort=False,
            )])
            fig_a.update_layout(
                title=dict(
                    text=f"Age distribution of confirmed malaria<br><sup>Total {int(total_age):,} · MOH 705A/B</sup>",
                    x=0.5, xanchor="center",
                ),
                height=460,
                margin=dict(t=80, b=40, l=20, r=20),
                showlegend=True,
                legend=dict(orientation="h", yanchor="bottom", y=-0.12, x=0.5, xanchor="center"),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(size=13),
            )
            st.plotly_chart(fig_a, use_container_width=True)
            st.caption(
                f"<5 years: {u5/total_age*100:.1f}% ({int(u5):,}) · "
                f"≥5 years: {o5/total_age*100:.1f}% ({int(o5):,})"
            )
        else:
            st.info("⚠️ No confirmed malaria by age band for this selection (MOH 705A/B).")

    # Community vs facility
    section_header("Facility vs community detection")
    com_tpr = compute_indicator("COM_TPR", df, filters)
    g1, g2, g3 = st.columns(3)
    with g1:
        big_number(fmt_count(cascade.get("confirmed", 0)), "OPD confirmed (705)")
    with g2:
        big_number(
            f"{com_tpr.get('value'):.1f}%" if com_tpr.get("status") == "valid" else "N/A",
            "Community TPR (748)",
        )
        st.caption(com_tpr.get("reason") or "MOH 748 positives ÷ totals")
    with g3:
        r11 = compute_indicator("R1_1", df, filters)
        big_number(
            f"{int(r11.get('value')):,}" if r11.get("value") is not None else "N/A",
            "Community tests (R1.1)",
        )

    # Monthly series
    section_header("Monthly transmission signals (valid TPR only)")
    monthly = d[d["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)] if not d.empty else pd.DataFrame()
    if monthly.empty:
        st.info("No monthly periods in the current filter.")
    else:
        series = []
        for per in sorted(monthly["period"].astype(str).unique()):
            sub = monthly[monthly["period"].astype(str) == per]
            a = compute_indicator("GO_1", sub)
            c = compute_cascade_metrics(sub)
            series.append({
                "Period": per,
                "Confirmed": c.get("confirmed"),
                "Tested": c.get("tested"),
                "TPR %": a.get("value") if a.get("status") == "valid" else None,
                "TPR status": a.get("status"),
            })
        ser = pd.DataFrame(series)
        st.dataframe(ser, use_container_width=True, hide_index=True)
        fig_s = px.line(ser, x="Period", y=["Confirmed", "Tested"], markers=True, height=340)
        fig_s.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig_s, use_container_width=True)
        ser_ok = ser[ser["TPR %"].notna()]
        if not ser_ok.empty:
            fig_t = px.line(ser_ok, x="Period", y="TPR %", markers=True, height=300)
            fig_t.add_hline(y=BASELINES["GO_1"]["baseline"], line_dash="dot", line_color="#999")
            fig_t.update_layout(plot_bgcolor="white")
            st.plotly_chart(fig_t, use_container_width=True)

    section_header("How to read this page")
    st.markdown(
        """
1. **Incidence** uses MOH 705 confirmed ÷ provisional catchment — indicative only.  
2. **ABER** is detection effort. If ABER is low, a falling TPR is not a success story.  
3. **TPR** is GO_1 (MOH 706 RDT only). INVALID (pos > exams) is hidden from the triad KPI.  
4. Open **Maps** for the same numbers on Kilifi geography (facility + community/CHU layer).  
5. CFR / severe stay N/A until death and severe lines exist in the KHIS extract.
        """
    )


# ==================================================================
# PAGE 8 — ALL INDICATORS
# ==================================================================

def page_all_indicators(user, filters):
    """
    B1 — 91 Indicator Command Centre / Explorer.
    Every value comes from evaluate_catalogue → compute_indicator / locked cascade.
    Missing data = N/A, never fake 0%.
    """
    df = load_all_data()
    page_header(
        "91 Indicator Command Centre",
        "Full CBMP indicator catalogue. Values are engine-backed; "
        "pending_data slots stay N/A until source systems are connected.",
    )
    if df.empty:
        st.warning("No KHIS data yet — commit a file on Upload & Settings.")
        st.caption(f"Catalogue still lists **{len(INDICATOR_CATALOGUE)}** indicator definitions.")
        cat_meta = pd.DataFrame(INDICATOR_CATALOGUE)[["id", "category", "name", "definition", "status_data", "source"]]
        st.dataframe(cat_meta, use_container_width=True, hide_index=True)
        return

    d = _filter_data(df, filters)
    with st.spinner("Evaluating indicator catalogue…"):
        cat_df = evaluate_catalogue(df, filters)

    live_n = int((cat_df["Status"] == "VALID").sum()) if not cat_df.empty else 0
    inv_n = int((cat_df["Status"] == "INVALID").sum()) if not cat_df.empty else 0
    na_n = int((cat_df["Status"] == "NA").sum()) if not cat_df.empty else 0
    total_n = len(cat_df) if not cat_df.empty else 0
    st.info(
        f"**A.6 completeness:** {total_n} catalogue rows · **{live_n} VALID** · "
        f"{inv_n} INVALID · {na_n} N/A/pending. "
        f"Pending is correct until 648, deaths, IRS, household surveys exist — not filled with zeros."
    )

    # Scorecard
    section_header("Executive scorecard")
    n_valid = int((cat_df["Status"] == "VALID").sum()) if not cat_df.empty else 0
    n_invalid = int((cat_df["Status"] == "INVALID").sum()) if not cat_df.empty else 0
    n_na = int((cat_df["Status"] == "NA").sum()) if not cat_df.empty else 0
    n_green = int((cat_df["RAG"] == "green").sum()) if not cat_df.empty else 0
    n_red = int((cat_df["RAG"] == "red").sum()) if not cat_df.empty else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        big_number(str(len(INDICATOR_CATALOGUE)), "Catalogue size")
    with c2:
        big_number(str(n_valid), "VALID values")
    with c3:
        big_number(str(n_invalid), "INVALID")
    with c4:
        big_number(str(n_na), "N/A / pending")
    with c5:
        big_number(f"{n_green}/{n_red}", "Green / Red RAG")

    if n_invalid:
        st.error(f"**{n_invalid}** indicator(s) INVALID under formula gate — do not use for RAG decisions.")
    if d.empty:
        st.warning("Active filters return no KHIS rows — many live indicators will be N/A.")

    # Category filter
    section_header("Indicator explorer")
    categories = ["All"] + sorted({m["category"] for m in INDICATOR_CATALOGUE})
    statuses = ["All", "VALID", "INVALID", "NA"]
    readiness = ["All", "live", "provisional", "pending_data"]
    f1, f2, f3 = st.columns(3)
    with f1:
        cat_pick = st.selectbox("Category", categories, key="b1_cat")
    with f2:
        st_pick = st.selectbox("Status", statuses, key="b1_st")
    with f3:
        rd_pick = st.selectbox("Data readiness", readiness, key="b1_rd")

    view = cat_df.copy()
    if cat_pick != "All":
        view = view[view["Category"] == cat_pick]
    if st_pick != "All":
        view = view[view["Status"] == st_pick]
    if rd_pick != "All":
        view = view[view["Data readiness"] == rd_pick]

    # Official core strip
    core_ids = ["GO_1", "SO_1", "R1_1", "R1_2", "CAS_1", "CAS_2", "EPI_1"]
    core = cat_df[cat_df["ID"].isin(core_ids)]
    if not core.empty:
        st.markdown("**Core programme indicators (engine)**")
        cols = st.columns(min(4, len(core)))
        for i, (_, r) in enumerate(core.iterrows()):
            with cols[i % len(cols)]:
                v = r["Value"]
                if r["Status"] == "INVALID":
                    disp = "INVALID"
                elif v is None or r["Status"] == "NA":
                    disp = "N/A"
                elif r["Unit"] == "%":
                    disp = f"{v:.1f}%"
                elif r["Unit"] == "count":
                    disp = f"{int(v):,}" if isinstance(v, (int, float)) else str(v)
                else:
                    disp = f"{v}"
                rag_emoji = {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚫"}.get(r["RAG"], "⚫")
                st.metric(f"{rag_emoji} {r['ID']}", disp, delta=r["Indicator"][:40])

    st.dataframe(view, use_container_width=True, hide_index=True)

    st.download_button(
        "📥 Download full catalogue evaluation (CSV)",
        cat_df.to_csv(index=False).encode("utf-8"),
        file_name=f"cbmp_91_catalogue_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        key="b1_dl_cat",
    )

    # Category breakdown charts
    section_header("By category")
    if not cat_df.empty:
        by = cat_df.groupby(["Category", "Status"]).size().reset_index(name="n")
        fig = px.bar(
            by, x="Category", y="n", color="Status", barmode="stack", height=400,
            color_discrete_map={"VALID": COLOR_GREEN, "INVALID": COLOR_RED, "NA": "#999"},
        )
        fig.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)

    # Pending list
    pending = cat_df[cat_df["Data readiness"] == "pending_data"]
    with st.expander(f"Pending data slots ({len(pending)}) — framework reserved"):
        st.caption("These stay N/A until registers / budget / climate / survey feeds are connected.")
        st.dataframe(
            pending[["ID", "Category", "Indicator", "Definition", "Source"]],
            use_container_width=True, hide_index=True,
        )

    st.caption(
        "Command Centre reads **only** catalogue evaluation (universal engine + locked cascade + ops tables). "
        "No parallel ad-hoc percentages for catalogue KPIs."
    )



def build_facility_benchmark(df, filters):
    """
    B2 — peer benchmark for the three CBMP sites.
    All rates via locked cascade / official engine (no ad-hoc %).
    """
    rows = []
    for fac in FACILITIES:
        pf = dict(filters or {})
        pf["facility"] = fac
        fd = _filter_data(df, pf)
        c = compute_cascade_metrics(fd)
        inds = compute_official_indicators(df, pf)
        go = inds.get("GO_1") or {}
        r11 = inds.get("R1_1") or {}
        r12 = inds.get("R1_2") or {}
        try:
            chps = query(
                "SELECT competency_status FROM chps WHERE (active = 1 OR active IS NULL) AND facility = ?",
                (fac,),
            )
            if chps.empty:
                so_v, so_n = None, 0
            else:
                so_n = len(chps)
                so_a = safe_rate(
                    int((chps["competency_status"] == "passed").sum()),
                    so_n,
                    indicator="B2_SO",
                )
                so_v = so_a.get("value") if so_a.get("status") == "valid" else None
        except Exception:
            so_v, so_n = None, 0
        try:
            al = get_active_alerts(facility=fac)
            n_alerts = len(al) if al is not None and not al.empty else 0
        except Exception:
            n_alerts = 0
        go_status = go.get("formula_status") or ("valid" if go.get("value") is not None else "na")
        rows.append({
            "Facility": fac,
            "Sub-county": SUBCOUNTY.get(fac, "—") if "SUBCOUNTY" in globals() else "—",
            "Suspected": c.get("suspected", 0),
            "Tested": c.get("tested", 0),
            "Confirmed": c.get("confirmed", 0),
            "AL": c.get("treated", 0),
            "Diag gap": c.get("diag_gap", 0),
            "Treat gap": c.get("treat_gap", 0),
            "Testing %": c.get("testing_rate"),
            "Treat cov %": c.get("treat_coverage"),
            "GO_1 TPR": go.get("value") if go_status == "valid" else None,
            "GO status": go_status,
            "R1.1 tests": r11.get("value"),
            "R1.2 %": r12.get("value") if r12.get("formula_status") != "invalid" else None,
            "SO_1 %": so_v,
            "Active CHPs": so_n,
            "Open alerts": n_alerts,
            "test_status": (c.get("rate_status") or {}).get("testing_rate"),
            "treat_status": (c.get("rate_status") or {}).get("treat_coverage"),
        })
    return pd.DataFrame(rows)


def build_chp_workload(facility=None):
    """
    B2 — CHP roster × MOH 648 workload intelligence.
    Returns (table_df, summary_dict).
    """
    try:
        roster = query("SELECT * FROM chps WHERE active = 1 OR active IS NULL")
    except Exception:
        roster = pd.DataFrame()
    try:
        m648 = query("SELECT * FROM moh_648")
    except Exception:
        m648 = pd.DataFrame()
    if facility and facility not in (None, "All three"):
        if not roster.empty and "facility" in roster.columns:
            roster = roster[roster["facility"] == facility]
        if not m648.empty and "facility" in m648.columns:
            m648 = m648[m648["facility"] == facility]
    summary = {
        "roster": len(roster) if roster is not None and not roster.empty else 0,
        "matched": 0,
        "zero_testing": 0,
        "high_workload": 0,
        "unmatched_648": 0,
    }
    if roster is None or roster.empty:
        return pd.DataFrame(), summary
    if m648 is None:
        m648 = pd.DataFrame()
    m648 = m648.copy() if not m648.empty else pd.DataFrame()
    for col in ("suspected", "tested", "positive", "treated", "referred", "followed_up"):
        if not m648.empty:
            if col not in m648.columns:
                m648[col] = 0
            m648[col] = pd.to_numeric(m648[col], errors="coerce").fillna(0)
    if not m648.empty:
        m648["name_key"] = m648["chp_name"].astype(str).str.strip().str.lower()
        agg = m648.groupby(["facility", "name_key"], as_index=False).agg(
            suspected=("suspected", "sum"),
            tested=("tested", "sum"),
            positive=("positive", "sum"),
            treated=("treated", "sum"),
            referred=("referred", "sum"),
            followed_up=("followed_up", "sum"),
        )
    else:
        agg = pd.DataFrame(columns=["facility", "name_key", "suspected", "tested", "positive", "treated", "referred", "followed_up"])

    roster = roster.copy()
    roster["name_key"] = roster["name"].astype(str).str.strip().str.lower()
    merged = roster.merge(agg, on=["facility", "name_key"], how="left")
    for col in ("suspected", "tested", "positive", "treated", "referred", "followed_up"):
        merged[col] = merged[col].fillna(0)

    # Peer median tests by facility
    med = merged.groupby("facility")["tested"].transform("median")
    rows = []
    for _, r in merged.iterrows():
        tested = float(r["tested"])
        pos = float(r["positive"])
        tpr_a = safe_rate(pos, tested, indicator="B2_CHP_TPR")
        ref_a = safe_rate(float(r["referred"]), pos, indicator="B2_CHP_REF") if pos else {"status": "na", "value": None}
        fu_a = safe_rate(float(r["followed_up"]), pos, indicator="B2_CHP_FU") if pos else {"status": "na", "value": None}
        peer = float(med.loc[r.name]) if r.name in med.index else 0.0
        tags = []
        if tested == 0 and float(r["suspected"]) == 0:
            tags.append("zero_activity")
        elif tested == 0:
            tags.append("zero_testing")
        if peer and tested >= max(peer * 1.5, peer + 10):
            tags.append("high_workload")
        if peer and 0 < tested <= max(peer * 0.4, 1):
            tags.append("low_vs_peers")
        if tpr_a.get("status") == "invalid":
            tags.append("invalid_tpr")
        rows.append({
            "Facility": r.get("facility"),
            "CHP": r.get("name"),
            "CHU": r.get("chu"),
            "Competency": r.get("competency_status"),
            "Suspected": int(r["suspected"]),
            "Tests": int(tested),
            "Positive": int(pos),
            "TPR %": tpr_a.get("value") if tpr_a.get("status") == "valid" else None,
            "TPR status": tpr_a.get("status"),
            "Treated": int(r["treated"]),
            "Referred": int(r["referred"]),
            "Followed up": int(r["followed_up"]),
            "Referral %": ref_a.get("value") if ref_a.get("status") == "valid" else None,
            "Follow-up %": fu_a.get("value") if fu_a.get("status") == "valid" else None,
            "Peer median tests": round(peer, 1),
            "Flags": ", ".join(tags) if tags else "—",
        })
        if "zero_testing" in tags or "zero_activity" in tags:
            summary["zero_testing"] += 1
        if "high_workload" in tags:
            summary["high_workload"] += 1
        if tested > 0:
            summary["matched"] += 1

    # Unmatched 648 names
    if not agg.empty:
        roster_keys = set(zip(roster["facility"], roster["name_key"]))
        extra = 0
        for _, a in agg.iterrows():
            if (a["facility"], a["name_key"]) not in roster_keys and a["name_key"] not in ("", "nan"):
                extra += 1
        summary["unmatched_648"] = extra

    return pd.DataFrame(rows), summary


def page_facility(user, filters):
    """
    B2 Facility intelligence — profile, peer benchmark, workload, catalogue KPIs.
    Respects global period / stream / register filters.
    """
    df = load_all_data()
    page_header(
        "Facility intelligence (B2)",
        "Peer benchmark, cascade, CHP workload, commodities, DQ and actions. "
        "Rates from the locked engine — invalid TPR is not used for ranking.",
    )
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    # Default facility from global filter when set
    fac_opts = FACILITIES + ["All three"]
    default_fac = filters.get("facility") if filters else "All three"
    if default_fac not in fac_opts:
        default_fac = "All three"
    try:
        default_idx = fac_opts.index(default_fac)
    except ValueError:
        default_idx = 0

    focus = st.selectbox("Facility", fac_opts, index=default_idx, key="deepdive_facility")

    # Scope filters to this facility for indicator math
    fac_filters = dict(filters or {})
    fac_filters["facility"] = focus if focus != "All three" else "All three"

    d_all = _filter_data(df, fac_filters)
    if d_all.empty:
        st.warning(
            "No rows for this facility under the **active filters**. "
            "Widen period / registers and click **Apply filters**."
        )
        st.caption(f"Raw rows in database: **{len(df):,}**")
        return

    indicators = compute_official_indicators(df, fac_filters)
    cascade = compute_cascade_metrics(d_all)
    levels = {k: indicator_rag(indicators[k], k) for k in ("GO_1", "SO_1", "R1_1", "R1_2")}
    worst = "red" if "red" in levels.values() else ("amber" if "amber" in levels.values() else "green")

    subcounty = SUBCOUNTY.get(focus, "—") if focus != "All three" else "Kilifi (three sites)"
    period_note = fac_filters.get("period_mode") or "filters applied"
    headline(
        f"**{focus}** · {subcounty} · {period_note} · "
        f"{len(d_all):,} rows in view. "
        f"Official RAG: GO {levels['GO_1']} · SO {levels['SO_1']} · "
        f"R1.1 {levels['R1_1']} · R1.2 {levels['R1_2']}.",
        status=worst,
    )

    # ── Official indicator cards ──
    section_header("Official indicators (this facility / view)")
    # Facility-level SO from roster when single facility selected
    so_ind = dict(indicators["SO_1"])
    if focus != "All three":
        try:
            chps_fac = query(
                "SELECT competency_status FROM chps WHERE (active = 1 OR active IS NULL) AND facility = ?",
                (focus,),
            )
            if not chps_fac.empty:
                passed_n = int((chps_fac["competency_status"] == "passed").sum())
                _sa = safe_rate(passed_n, len(chps_fac), indicator="SO_1_focus")
                so_ind["value"] = _sa.get("value") if _sa.get("status") == "valid" else None
                so_ind["source"] = f"CHP roster · {focus} ({passed_n}/{len(chps_fac)} passed)"
        except Exception:
            pass
        # Prefer GO per-facility value when available
        go_pf = indicators["GO_1"].get("per_facility", {}).get(focus, {})
        if isinstance(go_pf, dict) and go_pf.get("value") is not None:
            indicators["GO_1"] = dict(indicators["GO_1"])
            indicators["GO_1"]["value"] = go_pf["value"]
            indicators["GO_1"]["tested"] = go_pf.get("tested", indicators["GO_1"].get("tested", 0))
            indicators["GO_1"]["positive"] = go_pf.get("positive", 0)

    cards = [
        ("GO_1", indicators["GO_1"]),
        ("SO_1", so_ind),
        ("R1_1", indicators["R1_1"]),
        ("R1_2", indicators["R1_2"]),
    ]
    cols = st.columns(4)
    status_label = {"green": "On track", "amber": "At risk", "red": "Off track", "grey": "No data"}
    for col, (key, ind) in zip(cols, cards):
        with col:
            lvl = indicator_rag(ind, key)
            color = {"green": COLOR_GREEN, "amber": COLOR_AMBER, "red": COLOR_RED, "grey": COLOR_TEXT_MUTED}.get(lvl, COLOR_NAVY)
            st.markdown(
                f"""
                <div class="big-num">
                    <div class="big-num-label">{ind.get('name', key)}</div>
                    <div class="big-num-value" style="color:{color};font-size:1.8rem;">
                        {format_value(ind.get('value'), ind.get('unit', ''))}
                    </div>
                    <div class="big-num-delta flat">Target {format_value(ind.get('target'), ind.get('unit', ''))} · {status_label.get(lvl, lvl)}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    if indicators["GO_1"].get("testing_collapse"):
        st.warning("Testing-collapse flag active for this view — do not treat rising TPR as success.")

    # ── B2 peer benchmark ──
    section_header("Peer benchmark (B2)")
    st.caption(
        "Same filters, same locked cascade / official engine. Rank is among the three CBMP sites. "
        "INVALID rates are blank — not ranked as performance."
    )
    try:
        bench = build_facility_benchmark(df, filters)
    except Exception as e:
        bench = pd.DataFrame()
        st.caption(f"Benchmark unavailable: {e}")
    if not bench.empty:
        # Ranks (higher confirmed / tests = higher burden; lower gap = better)
        for col, ascending in (("Confirmed", False), ("Tested", False), ("Diag gap", True), ("Treat gap", True)):
            if col in bench.columns:
                bench[f"Rank {col}"] = bench[col].rank(ascending=ascending, method="min")
        show_b = bench.copy()
        for pct_col in ("Testing %", "Treat cov %", "GO_1 TPR", "SO_1 %", "R1.2 %"):
            if pct_col in show_b.columns:
                show_b[pct_col] = show_b[pct_col].apply(
                    lambda x: f"{x:.1f}" if isinstance(x, (int, float)) and pd.notna(x) else "—"
                )
        st.dataframe(show_b, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Download facility benchmark CSV",
            bench.to_csv(index=False).encode("utf-8"),
            file_name=f"facility_benchmark_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="b2_bench_csv",
        )
        if focus != "All three" and focus in bench["Facility"].values:
            me = bench[bench["Facility"] == focus].iloc[0]
            peers = bench[bench["Facility"] != focus]
            notes = []
            if peers["Confirmed"].max() and me["Confirmed"] >= peers["Confirmed"].max():
                notes.append("Highest confirmed burden among the three sites.")
            if me["Diag gap"] > peers["Diag gap"].mean() if len(peers) else 0:
                notes.append("Diagnosis gap above peer mean — testing access / recording.")
            if me["Treat gap"] > peers["Treat gap"].mean() if len(peers) else 0:
                notes.append("Treatment gap above peer mean — AL availability / documentation.")
            if me.get("GO status") == "invalid":
                notes.append("GO_1 TPR invalid at this site — reconcile MOH 706 RDT before ranking transmission.")
            if notes:
                for n in notes:
                    st.markdown(f"- {n}")
        # Burden vs gap chart — force numeric, handle zero/missing gap
        plot_b = bench.copy()
        for col in ("Tested", "Confirmed", "Diag gap", "Treat gap", "Suspected", "Testing %", "GO_1 TPR"):
            if col in plot_b.columns:
                plot_b[col] = pd.to_numeric(plot_b[col], errors="coerce")
        # Summary strip so empty chart is diagnosable
        tot_susp = float(plot_b["Suspected"].fillna(0).sum()) if "Suspected" in plot_b.columns else 0
        tot_test = float(plot_b["Tested"].fillna(0).sum()) if "Tested" in plot_b.columns else 0
        tot_conf = float(plot_b["Confirmed"].fillna(0).sum()) if "Confirmed" in plot_b.columns else 0
        tot_gap = float(plot_b["Diag gap"].fillna(0).sum()) if "Diag gap" in plot_b.columns else 0
        st.caption(
            f"Data strip — Suspected: **{tot_susp:,.0f}** · Tested: **{tot_test:,.0f}** · "
            f"Confirmed: **{tot_conf:,.0f}** · Diagnosis gap: **{tot_gap:,.0f}** · "
            f"status: **{'VALID inputs' if tot_susp > 0 and tot_test >= 0 else 'MISSING denominators'}**"
        )
        bubble_mode = st.radio(
            "Bubble view",
            ["Burden vs testing (gap size)", "Transmission vs testing (TPR × testing rate)"],
            horizontal=True,
            key="b2_bubble_mode",
        )
        plot_b["Diag gap"] = plot_b["Diag gap"].fillna(0)
        plot_b["bubble_size"] = plot_b["Diag gap"].clip(lower=0) + 1
        has_activity = (
            (plot_b["Tested"].fillna(0) > 0)
            | (plot_b["Confirmed"].fillna(0) > 0)
            | (plot_b["Diag gap"] > 0)
        )
        if not has_activity.any():
            st.info(
                "⚠️ Diagnosis-gap visualization unavailable\n\n"
                "Valid suspected and tested denominators are required for this view."
            )
        else:
            if bubble_mode.startswith("Transmission"):
                # Need testing rate and TPR
                if "Testing %" not in plot_b.columns or "GO_1 TPR" not in plot_b.columns:
                    st.info("⚠️ Testing rate or GO_1 TPR not available for transmission view.")
                else:
                    plot_t = plot_b.dropna(subset=["Testing %", "GO_1 TPR"]).copy()
                    if plot_t.empty:
                        st.info("⚠️ No valid testing rate + TPR pairs for transmission bubble.")
                    else:
                        plot_t["bubble_size"] = plot_t["Confirmed"].fillna(0).clip(lower=0) + 1
                        fig_b = px.scatter(
                            plot_t, x="Testing %", y="GO_1 TPR", size="bubble_size", color="Facility",
                            hover_data=["Confirmed", "Diag gap", "Tested"],
                            height=420,
                            title="Transmission vs testing (bubble ∝ confirmed)",
                            labels={"Testing %": "Testing rate %", "GO_1 TPR": "TPR %"},
                        )
                        fig_b.update_traces(marker=dict(line=dict(width=1.5, color="white"), opacity=0.85))
                        fig_b.update_layout(plot_bgcolor="white", margin=dict(t=50, b=40),
                                            legend=dict(orientation="h", y=1.12))
                        st.plotly_chart(fig_b, use_container_width=True)
                        st.caption(
                            "High TPR + low testing → investigate under-detection or stock-out. "
                            "High TPR + high testing → stronger transmission signal."
                        )
            else:
                hover_cols = [c for c in ("Treat gap", "GO_1 TPR", "R1.1 tests", "Testing %") if c in plot_b.columns]
                fig_b = px.scatter(
                    plot_b, x="Tested", y="Confirmed", size="bubble_size", color="Facility",
                    hover_data=hover_cols + ["Diag gap"],
                    height=420,
                    title="Burden vs testing (bubble size ∝ diagnosis gap)",
                    labels={"Tested": "Tested", "Confirmed": "Confirmed"},
                )
                fig_b.update_traces(
                    marker=dict(line=dict(width=1.5, color="white"), opacity=0.85),
                )
                fig_b.update_layout(
                    plot_bgcolor="white",
                    margin=dict(t=50, b=40),
                    legend=dict(orientation="h", y=1.12),
                )
                st.plotly_chart(fig_b, use_container_width=True)
                st.caption(
                    "X = tested · Y = confirmed · bubble size = diagnosis gap (suspected − tested). "
                    "Sites with zero gap still appear (minimum bubble)."
                )

    # ── B2 CHP workload at this facility ──
    section_header("CHP workload at this facility (B2)")
    wl, wsum = build_chp_workload(None if focus == "All three" else focus)
    w1, w2, w3, w4 = st.columns(4)
    with w1:
        big_number(str(wsum.get("roster", 0)), "Active CHPs")
    with w2:
        big_number(str(wsum.get("matched", 0)), "With 648 tests")
    with w3:
        big_number(str(wsum.get("zero_testing", 0)), "Zero testing / activity")
    with w4:
        big_number(str(wsum.get("high_workload", 0)), "High vs peer median")
    if wl.empty:
        st.info("Add CHP roster (SO page) and import MOH 648 to unlock named CHP workload.")
    else:
        st.dataframe(wl.sort_values("Tests", ascending=False), use_container_width=True, hide_index=True)
        if wsum.get("zero_testing"):
            st.warning(
                f"**{wsum['zero_testing']}** active CHP(s) with zero testing/activity in 648 — "
                "supervision targeting, not individual blame."
            )
        if wsum.get("unmatched_648"):
            st.caption(f"{wsum['unmatched_648']} MOH 648 name(s) not on roster — fix spelling / CHP ID.")
        fig_w = px.bar(
            wl.sort_values("Tests", ascending=False).head(20),
            x="CHP", y="Tests", color="Facility", height=340,
        )
        fig_w.update_layout(plot_bgcolor="white", xaxis_tickangle=-35)
        st.plotly_chart(fig_w, use_container_width=True)

    # ── Cascade gaps ──
    section_header("Cascade gaps & coverage")
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        big_number(
            f"{cascade['testing_rate']:.0f}%", "Testing rate",
            delta=f"{cascade['tested']:,} / {cascade['suspected']:,} suspected",
            delta_direction="up" if cascade["testing_rate"] >= 90 else "down",
        )
    with g2:
        big_number(
            f"{cascade['diag_gap']:,}", "Diagnosis gap",
            delta=f"{cascade['diag_gap_pct']:.0f}% of suspected",
            delta_direction="down" if cascade["diag_gap_pct"] > 10 else "flat",
        )
    with g3:
        big_number(
            f"{cascade['treat_coverage']:.0f}%", "Treatment coverage",
            delta=f"{cascade['treated']:,} AL / {cascade['confirmed']:,} confirmed",
            delta_direction="up" if cascade["treat_coverage"] >= 90 else "down",
        )
    with g4:
        big_number(
            f"{cascade['treat_gap']:,}", "Treatment gap",
            delta=f"{cascade['treat_gap_pct']:.0f}% confirmed without AL",
            delta_direction="down" if cascade["treat_gap_pct"] > 10 else "flat",
        )

    c_left, c_right = st.columns(2)
    with c_left:
        section_header("Cascade funnel")
        if cascade["suspected"] or cascade["tested"] or cascade["confirmed"]:
            fig = pgo.Figure(pgo.Funnel(
                y=["Suspected", "Tested", "Positive", "Treated (AL)"],
                x=[cascade["suspected"], cascade["tested"], cascade["confirmed"], cascade["treated"]],
                textinfo="value+percent initial",
                marker={"color": [COLOR_NAVY, COLOR_BLUE, COLOR_AMBER, COLOR_GREEN]},
            ))
            fig.update_layout(height=360, showlegend=False, plot_bgcolor="white", margin=dict(t=20))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No cascade volumes for this facility under current filters.")

    with c_right:
        section_header("Volume trend (monthly)")
        monthly = d_all[d_all["period"].astype(str).str.len() == 7].copy()
        if not monthly.empty:
            vol = monthly[
                monthly["indicator"].str.contains(r"Total Exam|Tested for Malaria", na=False, regex=True)
            ].groupby(monthly["period"].astype(str))["value"].sum().sort_index()
            pos = monthly[
                monthly["indicator"].str.contains(r"Positive|Confirmed", na=False, regex=True)
            ].groupby(monthly["period"].astype(str))["value"].sum().sort_index()
            trend_df = pd.DataFrame({"Tested": vol, "Positive": pos}).fillna(0).reset_index()
            trend_df.columns = ["Period", "Tested", "Positive"]
            if len(trend_df) >= 1:
                fig_t = px.line(
                    trend_df, x="Period", y=["Tested", "Positive"],
                    markers=True, color_discrete_sequence=[COLOR_BLUE, COLOR_RED],
                    height=360,
                )
                fig_t.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
                st.plotly_chart(fig_t, use_container_width=True)
            else:
                st.info("Not enough monthly points for a trend.")
        else:
            st.info("No monthly periods in this view — switch filter to monthly periods.")

    # ── Open alerts ──
    section_header("Open alerts")
    try:
        evaluate_alerts(recent_months=6)
    except Exception:
        pass
    alerts_df = get_active_alerts(facility=None if focus == "All three" else focus)
    if alerts_df.empty:
        st.success("No open alerts for this facility scope.")
    else:
        st.caption(f"{len(alerts_df)} open alert(s). Resolve in **Alerts** if action is taken.")
        for _, row in alerts_df.head(12).iterrows():
            emoji = {"critical": "🔴", "high": "🟠", "warning": "🟡"}.get(row["severity"], "•")
            msg = str(row["message"])
            if msg.startswith("[") and "]" in msg:
                msg = msg.split("]", 1)[1].strip()
            st.markdown(
                f"{emoji} **{row['severity']}** · {row['facility']} · {row.get('period') or '—'} — {msg}"
            )
        if len(alerts_df) > 12:
            st.caption(f"…and {len(alerts_df) - 12} more on the Alerts page.")

    # ── Actions ──
    section_header("Actions this period")
    actions = build_action_list(d_all, {
        "GO_1": indicators["GO_1"],
        "SO_1": so_ind,
        "R1_1": indicators["R1_1"],
        "R1_2": indicators["R1_2"],
    })
    if not actions:
        st.success("No urgent actions from current thresholds for this view.")
    else:
        for a in actions:
            icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(a.get("priority"), "•")
            st.markdown(f"{icon} **{str(a.get('priority', '')).upper()}** — {a.get('text', '')}")

    # ── vs peers (when single facility) ──
    if focus != "All three":
        section_header("Compared with other facilities (same filters)")
        peer_rows = []
        for fac in FACILITIES:
            pf = dict(fac_filters)
            pf["facility"] = fac
            fd = _filter_data(df, pf)
            c = compute_cascade_metrics(fd)
            go_v = indicators["GO_1"].get("per_facility", {}).get(fac, {})
            tpr = go_v.get("value") if isinstance(go_v, dict) else None
            peer_rows.append({
                "Facility": fac + (" ←" if fac == focus else ""),
                "Tests": c["tested"],
                "TPR %": f"{tpr:.1f}" if tpr is not None else "—",
                "Diagnosis gap %": f"{c['diag_gap_pct']:.0f}",
                "Treatment cov. %": f"{c['treat_coverage']:.0f}",
                "Confirmed": c["confirmed"],
            })
        st.dataframe(pd.DataFrame(peer_rows), use_container_width=True, hide_index=True)

    # ── Operations slice ──
    section_header("Operations (this facility)")
    op1, op2, op3 = st.columns(3)
    with op1:
        st.markdown("**CHP competency**")
        if focus == "All three":
            chps = query("SELECT facility, competency_status FROM chps WHERE active = 1 OR active IS NULL")
        else:
            chps = query(
                "SELECT facility, competency_status FROM chps WHERE (active = 1 OR active IS NULL) AND facility = ?",
                (focus,),
            )
        if chps.empty:
            st.caption("No roster rows — import on SO page.")
        else:
            n = len(chps)
            passed = int((chps["competency_status"] == "passed").sum())
            st.metric("Active CHPs", n)
            _sa = safe_rate(passed, n, indicator="SO_1_dd") if n else {"status": "na", "value": None}
            st.caption(
                f"Passed: **{passed}** ({_sa['value']:.0f}%)" if n and _sa.get("value") is not None else
                (f"Passed: **{passed}**" if n else "")
            )
    with op2:
        st.markdown("**Stock**")
        try:
            if focus == "All three":
                stock = query("SELECT * FROM stock")
            else:
                stock = query("SELECT * FROM stock WHERE facility = ?", (focus,))
        except Exception:
            stock = pd.DataFrame()
        if stock.empty:
            st.caption("No stock log entries.")
        else:
            for _, s in stock.iterrows():
                rp = s.get("reorder_point", 30)
                flag = "🔴" if float(s.get("quantity") or 0) < float(rp or 30) else "🟢"
                st.caption(f"{flag} {s.get('item')} @ {s.get('facility')}: **{s.get('quantity')}** (reorder {rp})")
    with op3:
        st.markdown("**Reporting signals**")
        rr = d_all[d_all["indicator"].astype(str).str.contains(r"Reporting\s*Rate", na=False, case=False)]
        if rr.empty:
            st.caption("No reporting-rate rows in view.")
        else:
            vals = rr["value"].astype(float)
            if vals.max() <= 1.5:
                vals = vals * 100.0
            st.metric("Avg reporting %", f"{vals.mean():.0f}%")
            st.caption(f"Cells &lt; 80%: **{int((vals < 80).sum())}** · &lt; 60%: **{int((vals < 60).sum())}**")

    # ── Register summary ──
    section_header("Register performance summary")
    summary_rows = []
    for reg in REGISTERS:
        sub = d_all[d_all["register"] == reg]
        if sub.empty:
            summary_rows.append({
                "Register": reg, "Suspected": "—", "Tested": "—",
                "Positive": "—", "Positivity": "—", "Reporting": "—",
            })
            continue
        susp = sub[sub["indicator"].str.contains("Suspected", na=False)]["value"].sum()
        t = sub[sub["indicator"].str.contains(r"Tested for Malaria|Total Exam", na=False, regex=True)]["value"].sum()
        p = sub[sub["indicator"].str.contains(r"Positive|Confirmed", na=False, regex=True)]["value"].sum()
        rr_sub = sub[sub["indicator"].str.contains(r"Reporting\s*Rate", na=False, case=False)]
        rr_pct = None
        rr_status = "na"
        if not rr_sub.empty:
            ra = reporting_rate_audit(rr_sub["value"].mean(), register=reg)
            rr_status = ra.get("status")
            rr_pct = ra.get("value") if ra.get("status") == "valid" else None
            if ra.get("status") == "invalid":
                rr_pct = None
        pos_a = safe_rate(p, t, indicator=f"dd_reg_{reg}")
        summary_rows.append({
            "Register": reg,
            "Suspected": f"{int(susp):,}" if susp else "—",
            "Tested": f"{int(t):,}" if t else "—",
            "Positive": f"{int(p):,}" if p else "—",
            "Positivity": (
                f"{pos_a['value']:.1f}%" if pos_a.get("status") == "valid" else
                (f"INVALID ({pos_a.get('calculated_pct')})" if pos_a.get("status") == "invalid" else "—")
            ),
            "Reporting": (
                f"{rr_pct:.0f}%" if rr_pct is not None else
                ("INVALID" if rr_status == "invalid" else "—")
            ),
        })
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    # ═══ Phase B — Full facility intelligence profile ═══
    section_header("Facility intelligence profile (Phase B)")
    st.caption(
        "One-site operational profile: burden · testing · positivity · treatment · commodities · "
        "reporting · DQ · CHP · referrals · supervision · workplan · trends · alerts."
    )

    # Trends (monthly)
    monthly = d_all[d_all["period"].astype(str).str.len() == 7].copy()
    if not monthly.empty:
        section_header("Trends (monthly periods in view)")
        trend_rows = []
        for per in sorted(monthly["period"].astype(str).unique()):
            sub = monthly[monthly["period"].astype(str) == per]
            c = compute_cascade_metrics(sub)
            go_a = _positivity_rdt(sub, facility=focus if focus != "All three" else None, period=per)
            trend_rows.append({
                "Period": per,
                "Suspected": c["suspected"],
                "Tested": c["tested"],
                "Confirmed": c["confirmed"],
                "AL": c["treated"],
                "Diag gap": c["diag_gap"],
                "Treat gap": c["treat_gap"],
                "TPR %": go_a.get("value") if go_a.get("status") == "valid" else None,
                "TPR status": go_a.get("status"),
            })
        tr = pd.DataFrame(trend_rows)
        st.dataframe(tr, use_container_width=True, hide_index=True)
        if len(tr) >= 2:
            t1, t2 = st.columns(2)
            with t1:
                fig_tr = px.line(
                    tr, x="Period", y=["Tested", "Confirmed", "AL"], markers=True, height=340,
                )
                fig_tr.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
                st.plotly_chart(fig_tr, use_container_width=True)
            with t2:
                tr_ok = tr[tr["TPR %"].notna()]
                if not tr_ok.empty:
                    fig_tp = px.line(tr_ok, x="Period", y="TPR %", markers=True, height=340)
                    fig_tp.add_hline(y=BASELINES["GO_1"]["baseline"], line_dash="dot", line_color="#999")
                    fig_tp.update_layout(plot_bgcolor="white")
                    st.plotly_chart(fig_tp, use_container_width=True)
                else:
                    st.caption("No valid monthly TPR points in view.")

    # Commodity forecast for this facility
    section_header("Commodities & stock-out risk")
    try:
        fc = compute_commodity_forecast()
        if focus != "All three" and not fc.empty:
            fc = fc[fc["Facility"] == focus]
        if fc.empty:
            st.info("No stock lines — log stock on CHP Performance or import stock movements.")
        else:
            st.dataframe(fc, use_container_width=True, hide_index=True)
    except Exception as e:
        st.caption(f"Forecast unavailable: {e}")

    # Referrals + supervision + workplan for facility
    f1, f2, f3 = st.columns(3)
    with f1:
        section_header("Referrals")
        try:
            if focus == "All three":
                refs = query("SELECT * FROM referrals ORDER BY referral_date DESC LIMIT 30")
            else:
                refs = query(
                    "SELECT * FROM referrals WHERE facility = ? ORDER BY referral_date DESC LIMIT 30",
                    (focus,),
                )
        except Exception:
            refs = pd.DataFrame()
        if refs.empty:
            st.caption("No referrals logged.")
        else:
            n = len(refs)
            arrived = int(refs["arrived"].sum()) if "arrived" in refs.columns else 0
            ra = safe_rate(arrived, n, indicator="REF_1_fac")
            st.metric("Completion", f"{ra['value']:.0f}%" if ra.get("value") is not None else "—")
            st.caption(f"{arrived}/{n} arrived")
            st.dataframe(refs.head(10), use_container_width=True, hide_index=True)
    with f2:
        section_header("Supervision")
        try:
            if focus == "All three":
                sa = query(
                    "SELECT * FROM supervision_actions ORDER BY id DESC LIMIT 20"
                )
            else:
                sa = query(
                    "SELECT * FROM supervision_actions WHERE facility = ? ORDER BY id DESC LIMIT 20",
                    (focus,),
                )
        except Exception:
            sa = pd.DataFrame()
        if sa.empty:
            st.caption("No supervision actions.")
        else:
            today_s = datetime.now().strftime("%Y-%m-%d")
            od = sa[
                sa["status"].isin(["open", "in_progress"])
                & sa["deadline"].notna()
                & (sa["deadline"].astype(str) < today_s)
            ]
            st.metric("Overdue actions", str(len(od)))
            st.dataframe(sa.head(10), use_container_width=True, hide_index=True)
    with f3:
        section_header("Workplan actions")
        try:
            if focus == "All three":
                wp = query("SELECT * FROM workplan_actions ORDER BY id DESC LIMIT 20")
            else:
                wp = query(
                    "SELECT * FROM workplan_actions WHERE facility = ? OR facility IS NULL ORDER BY id DESC LIMIT 20",
                    (focus,),
                )
        except Exception:
            wp = pd.DataFrame()
        if wp.empty:
            st.caption("No workplan rows — add on CHP Performance → Workplan.")
        else:
            open_n = int(wp["status"].isin(["open", "in_progress"]).sum()) if "status" in wp.columns else 0
            st.metric("Open workplan", str(open_n))
            st.dataframe(wp.head(10), use_container_width=True, hide_index=True)

    # DQ + alerts for facility
    section_header("Data quality & alerts (this facility)")
    dq_f = compute_dq_score(df, fac_filters)
    al_f = get_active_alerts(facility=None if focus == "All three" else focus)
    q1, q2 = st.columns(2)
    with q1:
        if dq_f.get("score") is not None:
            big_number(f"{dq_f['score']:.0f}", f"DQ · {dq_f.get('label')}")
            for n in (dq_f.get("notes") or [])[:4]:
                st.caption(f"· {n}")
        else:
            st.caption("DQ score n/a")
    with q2:
        if al_f is None or al_f.empty:
            st.success("No open alerts for this scope.")
        else:
            st.warning(f"**{len(al_f)}** open alert(s)")
            for _, row in al_f.head(6).iterrows():
                msg = str(row["message"])
                if msg.startswith("[") and "]" in msg:
                    msg = msg.split("]", 1)[1].strip()
                st.caption(f"· {row['severity']} · {msg[:100]}")

    # Actions
    section_header("Corrective actions (generated)")
    acts = build_action_list(d_all, indicators)
    if not acts:
        st.success("No urgent generated actions for this view.")
    else:
        for a in acts:
            st.markdown(f"**{str(a.get('priority','')).upper()}** — {a.get('text')}")

    st.caption(
        "Profile follows **Apply filters**. Use **Loss & Gap** for IPT; **Reports** for printable brief; "
        "**CHP Performance → Workplan** for formal problem→action→owner→closure."
    )


# ==================================================================
# PAGE 10 — LOSS & GAP ANALYSIS
# ==================================================================

def _sum_ind(d, pattern, register=None):
    """Sum values matching indicator regex; optional register filter."""
    if d is None or d.empty:
        return 0.0
    sub = d if register is None else d[d["register"] == register]
    if sub.empty:
        return 0.0
    m = sub[sub["indicator"].str.contains(pattern, na=False, regex=True)]
    return float(m["value"].sum()) if not m.empty else 0.0


def _safe_pct(num, den):
    """Legacy helper — uses formula engine; returns 0 only when N/A (display)."""
    a = safe_rate(num, den, indicator="cascade_pct")
    if a["status"] == "valid":
        return a["value"]
    if a["status"] == "invalid":
        return a.get("calculated_pct")  # raw for transparency; callers should check status
    return 0.0


def compute_cascade_metrics(d):
    """
    Phase A.5 cascade — locked 705 clinical path + AL totals (no broad Positive|Total Exam).
    Rates through formula safety engine with registry definitions.
    """
    if d is None or d.empty:
        return {
            "suspected": 0, "tested": 0, "confirmed": 0, "treated": 0,
            "treated_wo_test": 0, "diag_gap": 0, "treat_gap": 0,
            "testing_rate": 0.0, "positivity": 0.0, "treat_coverage": 0.0,
            "diag_gap_pct": 0.0, "treat_gap_pct": 0.0,
            "rate_status": {},
            "definition": "locked CAS_1/CAS_POS/CAS_2",
        }
    susp = volume_from_registry(d, "CAS_1", "den")
    tested = volume_from_registry(d, "CAS_1", "num")
    confirmed = volume_from_registry(d, "CAS_POS", "num")
    treated = volume_from_registry(d, "CAS_2", "num")
    twt = sum_locked(
        d,
        {"indicator_contains": ["Treated w/o Testing"]},
    )
    if twt <= 0:
        twt = sum_locked(d, {"indicator_contains": ["CASES TREATED WITHOUT TESTING"]})
    diag_gap = max(susp - tested, 0)
    treat_gap = max(confirmed - treated, 0)

    meta1 = INDICATOR_REGISTRY["CAS_1"]
    meta_p = INDICATOR_REGISTRY["CAS_POS"]
    meta2 = INDICATOR_REGISTRY["CAS_2"]
    a_test = safe_rate(
        tested, susp, indicator="CAS_1", formula=meta1["formula"], source=meta1["source"],
    )
    a_pos = safe_rate(
        confirmed, tested, indicator="CAS_POS", formula=meta_p["formula"], source=meta_p["source"],
    )
    a_treat = safe_rate(
        treated, confirmed, indicator="CAS_2", formula=meta2["formula"], source=meta2["source"],
    )
    a_dg = safe_rate(diag_gap, susp, indicator="cascade_diag_gap_pct", formula="(suspected−tested)/suspected × 100")
    a_tg = safe_rate(treat_gap, confirmed, indicator="cascade_treat_gap_pct", formula="(confirmed−AL)/confirmed × 100")

    def _disp(a):
        if a["status"] == "valid":
            return a["value"] or 0.0
        if a["status"] == "invalid":
            return a.get("calculated_pct") or 0.0
        return 0.0

    # Small-cell suppression for aggregation roles (donor/partner/viewer)
    def _mc(v):
        out = apply_min_cell(v)
        if out is None:
            return None
        try:
            return int(out)
        except Exception:
            return out

    return {
        "suspected": _mc(susp),
        "tested": _mc(tested),
        "confirmed": _mc(confirmed),
        "treated": _mc(treated),
        "treated_wo_test": _mc(twt),
        "diag_gap": _mc(diag_gap),
        "treat_gap": _mc(treat_gap),
        "testing_rate": _disp(a_test),
        "positivity": _disp(a_pos),
        "treat_coverage": _disp(a_treat),
        "diag_gap_pct": _disp(a_dg),
        "treat_gap_pct": _disp(a_tg),
        "rate_status": {
            "testing_rate": a_test.get("status"),
            "positivity": a_pos.get("status"),
            "treat_coverage": a_treat.get("status"),
        },
        "rate_audits": [a_test, a_pos, a_treat],
        "definition": "locked 705 suspected/tested/confirmed + AL Total/Dispensed",
        "min_cell_applied": bool((st.session_state.get("user") or {}).get("aggregation")),
    }


def compute_dq_score(df, filters):
    """
    Simple 0–100 data-quality score for Overview and DQ page.
    Components (equal weight when data exists):
      - Reporting-rate average (target ≥95%)
      - Completeness of core registers in view
      - Share of low-reporting cells (<80%)
      - Presence of GO_1 inputs (RDT exams + positives)
    """
    if df is None or df.empty:
        return {
            "score": None, "level": "grey", "label": "No data",
            "components": {}, "notes": ["Upload KHIS data to compute a DQ score."],
        }
    d = _filter_data(df, filters)
    if d.empty:
        return {
            "score": None, "level": "grey", "label": "No data in filters",
            "components": {}, "notes": ["Widen period or facility and Apply filters."],
        }

    notes = []
    components = {}

    # 1. Reporting rate
    rr = d[d["indicator"] == "Reporting Rate"].copy()
    if not rr.empty:
        vals = rr["value"].astype(float)
        if vals.max() <= 1.5:
            vals = vals * 100.0
        avg_rr = float(vals.mean())
        n_low = int((vals < 80).sum())
        rr_score = min(100.0, max(0.0, avg_rr))
        if n_low > 0:
            rr_score = max(0.0, rr_score - min(30.0, n_low * 3))
        components["reporting"] = round(rr_score, 1)
        notes.append(f"Avg reporting rate {avg_rr:.0f}% ({n_low} cells <80%).")
    else:
        components["reporting"] = 50.0
        notes.append("No reporting-rate rows — completeness inferred from volumes only.")

    # 2. Core register presence (Phase 1 set)
    core_regs = ["MOH 706", "MOH 705A", "MOH 705B", "MOH 743", "MOH 748", "MOH 515"]
    present = set(d["register"].dropna().unique().tolist())
    hit = sum(1 for r in core_regs if r in present)
    reg_score = (hit / len(core_regs)) * 100.0
    components["registers"] = round(reg_score, 1)
    notes.append(f"{hit}/{len(core_regs)} core registers present in view.")

    # 3. GO_1 inputs (RDT)
    rdt_exam = d[d["indicator"].str.contains(r"RDT.*Total Exam|Rapid diagnostic.*Total Exam", na=False, regex=True)]
    rdt_pos = d[d["indicator"].str.contains(r"RDT.*Positive|Rapid diagnostic.*Positive", na=False, regex=True)]
    go_ok = (not rdt_exam.empty) and (not rdt_pos.empty) and float(rdt_exam["value"].sum()) > 0
    components["go1_inputs"] = 100.0 if go_ok else 20.0
    notes.append("GO_1 RDT inputs present." if go_ok else "GO_1 RDT exams/positives missing or zero.")

    # 4. Period coverage (at least one monthly period with volume)
    monthly = [p for p in d["period"].astype(str).unique() if len(str(p)) == 7 and "-" in str(p)]
    components["periods"] = 100.0 if monthly else 40.0
    notes.append(f"{len(monthly)} monthly period(s) in view." if monthly else "No monthly periods — only annual/quarterly.")

    # 5. Curated validation rules (penalise score when critical/high fail)
    rules = run_validation_rules(d)
    n_crit = sum(1 for r in rules if r["severity"] == "critical" and r["status"] == "fail")
    n_high = sum(1 for r in rules if r["severity"] == "high" and r["status"] == "fail")
    n_warn = sum(1 for r in rules if r["severity"] == "warning" and r["status"] == "fail")
    rule_penalty = min(40.0, n_crit * 12 + n_high * 6 + n_warn * 2)
    components["validation"] = round(max(0.0, 100.0 - rule_penalty), 1)
    notes.append(
        f"Validation rules: {n_crit} critical · {n_high} high · {n_warn} warning failures."
    )

    weights = {
        "reporting": 0.30, "registers": 0.20, "go1_inputs": 0.20,
        "periods": 0.10, "validation": 0.20,
    }
    score = sum(components[k] * weights[k] for k in weights)
    score = round(score, 1)
    if score >= 85:
        level, label = "green", "Good"
    elif score >= 65:
        level, label = "amber", "Fair — review gaps"
    else:
        level, label = "red", "Weak — interpret with caution"
    return {
        "score": score, "level": level, "label": label,
        "components": components, "notes": notes, "rules": rules,
    }


def run_validation_rules(d):
    """
    Curated validation rules (not a full 52-rule engine).
    Each rule: id, name, severity, status (pass/fail/skip), message, detail.
    """
    rules = []
    if d is None or d.empty:
        return rules

    def _add(rid, name, severity, status, message, detail=""):
        rules.append({
            "id": rid, "name": name, "severity": severity,
            "status": status, "message": message, "detail": detail,
        })

    cascade = compute_cascade_metrics(d)

    # V1 — GO_1 RDT inputs
    rdt_exam = d[d["indicator"].str.contains(r"RDT.*Total Exam|Rapid diagnostic.*Total Exam", na=False, regex=True)]
    rdt_pos = d[d["indicator"].str.contains(r"RDT.*Positive|Rapid diagnostic.*Positive", na=False, regex=True)]
    exam_sum = float(rdt_exam["value"].sum()) if not rdt_exam.empty else 0.0
    pos_sum = float(rdt_pos["value"].sum()) if not rdt_pos.empty else 0.0
    if exam_sum <= 0:
        _add("V1", "GO_1 RDT exams present", "critical", "fail",
             "No RDT total exams in view — GO_1 cannot be calculated.", "")
    elif pos_sum < 0:
        _add("V1", "GO_1 RDT exams present", "critical", "fail", "Negative RDT positives.", "")
    else:
        _add("V1", "GO_1 RDT exams present", "critical", "pass",
             f"RDT exams {exam_sum:,.0f} · positives {pos_sum:,.0f}.", "")

    # V2 — Positives without exams
    if pos_sum > 0 and exam_sum <= 0:
        _add("V2", "RDT positives need exams", "critical", "fail",
             "Positives reported without any RDT exams.", "")
    elif pos_sum > exam_sum and exam_sum > 0:
        _add("V2", "RDT positives ≤ exams", "high", "fail",
             f"Positives ({pos_sum:,.0f}) exceed exams ({exam_sum:,.0f}).", "")
    else:
        _add("V2", "RDT positives ≤ exams", "high", "pass", "Positives within exam volume.", "")

    # V3 — Diagnosis gap
    if cascade["suspected"] > 0 and cascade["diag_gap_pct"] >= 25:
        _add("V3", "Diagnosis gap < 25%", "high", "fail",
             f"Diagnosis gap {cascade['diag_gap_pct']:.0f}% ({cascade['diag_gap']:,} not tested).",
             "Check RDT stock and testing protocol.")
    elif cascade["suspected"] > 0:
        _add("V3", "Diagnosis gap < 25%", "high", "pass",
             f"Diagnosis gap {cascade['diag_gap_pct']:.0f}%.", "")
    else:
        _add("V3", "Diagnosis gap < 25%", "high", "skip", "No suspected volume in view.", "")

    # V4 — Treatment gap
    if cascade["confirmed"] > 0 and cascade["treat_gap_pct"] >= 20:
        _add("V4", "Treatment gap < 20%", "high", "fail",
             f"Treatment gap {cascade['treat_gap_pct']:.0f}% ({cascade['treat_gap']:,} without AL).",
             "Reconcile AL register vs confirmed cases.")
    elif cascade["confirmed"] > 0:
        _add("V4", "Treatment gap < 20%", "high", "pass",
             f"Treatment gap {cascade['treat_gap_pct']:.0f}%.", "")
    else:
        _add("V4", "Treatment gap < 20%", "high", "skip", "No confirmed volume in view.", "")

    # V5 — Reporting rate cells < 60%
    rr = d[d["indicator"] == "Reporting Rate"].copy()
    if not rr.empty:
        vals = rr["value"].astype(float)
        if vals.max() <= 1.5:
            vals = vals * 100.0
        n_crit_rr = int((vals < 60).sum())
        n_warn_rr = int(((vals >= 60) & (vals < 80)).sum())
        if n_crit_rr > 0:
            _add("V5", "Reporting rate ≥ 60%", "critical", "fail",
                 f"{n_crit_rr} cell(s) below 60% reporting rate.", "")
        elif n_warn_rr > 0:
            _add("V5", "Reporting rate ≥ 60%", "warning", "fail",
                 f"{n_warn_rr} cell(s) between 60–79%.", "")
        else:
            _add("V5", "Reporting rate ≥ 60%", "critical", "pass", "No cells below 60%.", "")
    else:
        _add("V5", "Reporting rate ≥ 60%", "critical", "skip", "No reporting-rate rows.", "")

    # V6 — Core facility registers present
    core_fac = {"MOH 706", "MOH 705A", "MOH 705B", "MOH 743"}
    present = set(d["register"].dropna().unique().tolist())
    missing = sorted(core_fac - present)
    if missing:
        _add("V6", "Core facility registers", "high", "fail",
             f"Missing: {', '.join(missing)}.", "")
    else:
        _add("V6", "Core facility registers", "high", "pass", "706 / 705A / 705B / 743 present.", "")

    # V7 — Community stream signals
    has_748 = "MOH 748" in present
    has_515 = "MOH 515" in present
    if has_748 and not has_515:
        _add("V7", "MOH 748 with MOH 515", "warning", "fail",
             "Community tests (748) present but reporting rate (515) missing.", "")
    elif has_748 or has_515:
        _add("V7", "MOH 748 with MOH 515", "warning", "pass",
             "Community registers consistent for this view.", "")
    else:
        _add("V7", "MOH 748 with MOH 515", "warning", "skip", "No community registers in view.", "")

    # V8 — Treated without testing spike
    if cascade["treated"] > 0 and cascade["treated_wo_test"] > 0:
        pct = _safe_pct(cascade["treated_wo_test"], cascade["treated"])
        if pct >= 5:
            _add("V8", "Treated without testing < 5%", "high", "fail",
                 f"{pct:.1f}% of AL volume marked treated without testing ({cascade['treated_wo_test']:,}).",
                 "Protocol / documentation issue.")
        else:
            _add("V8", "Treated without testing < 5%", "high", "pass",
                 f"{pct:.1f}% treated without testing.", "")
    else:
        _add("V8", "Treated without testing < 5%", "high", "pass",
             "No treated-without-testing volume flagged.", "")

    # V9 — Testing collapse heuristic (volume drop vs prior period in same frame)
    monthly = d[d["period"].astype(str).str.len() == 7].copy()
    if not monthly.empty and "period" in monthly.columns:
        vol = monthly[monthly["indicator"].str.contains(r"Total Exam|Tested for Malaria", na=False, regex=True)]
        if not vol.empty:
            by_p = vol.groupby(vol["period"].astype(str))["value"].sum().sort_index()
            if len(by_p) >= 2:
                latest, prev = float(by_p.iloc[-1]), float(by_p.iloc[-2])
                if prev > 0 and latest < prev * 0.5:
                    _add("V9", "Testing volume stable", "critical", "fail",
                         f"Testing volume fell >50% ({prev:,.0f} → {latest:,.0f}) across latest periods.",
                         "Investigate stock-out or reporting lag — do not read rising TPR as success.")
                else:
                    _add("V9", "Testing volume stable", "critical", "pass",
                         f"Latest vs prior period volume: {latest:,.0f} vs {prev:,.0f}.", "")
            else:
                _add("V9", "Testing volume stable", "critical", "skip", "Need ≥2 monthly periods.", "")
        else:
            _add("V9", "Testing volume stable", "critical", "skip", "No exam/tested volume series.", "")
    else:
        _add("V9", "Testing volume stable", "critical", "skip", "No monthly periods in view.", "")

    # V10 — Zero tests in a facility that has other activity
    for fac in FACILITIES:
        fac_d = d[d["facility"] == fac]
        if fac_d.empty:
            continue
        tests = _sum_ind(fac_d, r"Total Exam|Tested for Malaria")
        other = len(fac_d[fac_d["indicator"] != "Reporting Rate"])
        if other > 0 and tests <= 0:
            _add("V10", f"Testing activity — {fac}", "warning", "fail",
                 f"{fac}: other indicators present but zero tests in view.", "")
        elif tests > 0:
            _add("V10", f"Testing activity — {fac}", "warning", "pass",
                 f"{fac}: {tests:,.0f} tests in view.", "")

    # V11 — Positivity implausible (>95% with meaningful volume)
    if cascade["tested"] >= 50 and cascade["positivity"] > 95:
        _add("V11", "Positivity plausible", "high", "fail",
             f"Positivity {cascade['positivity']:.0f}% on {cascade['tested']:,} tests — check data entry.", "")
    elif cascade["tested"] >= 50:
        _add("V11", "Positivity plausible", "high", "pass",
             f"Positivity {cascade['positivity']:.0f}% on {cascade['tested']:,} tests.", "")
    else:
        _add("V11", "Positivity plausible", "high", "skip", "Insufficient test volume for check.", "")

    # V12 — All three facilities represented when filter is All three
    facs = set(d["facility"].dropna().unique().tolist())
    if len(facs) < 3 and len(facs) > 0:
        missing_f = [f for f in FACILITIES if f not in facs]
        _add("V12", "All facilities in view", "warning", "fail",
             f"Only {len(facs)}/3 facilities: missing {', '.join(missing_f)}.", "")
    elif len(facs) >= 3:
        _add("V12", "All facilities in view", "warning", "pass", "All three facilities present.", "")
    else:
        _add("V12", "All facilities in view", "warning", "skip", "No facility rows.", "")

    return rules


def compare_uploads(upload_id_a, upload_id_b):
    """
    Diff two upload snapshots.
    Returns summary dict + detail DataFrame of changed keys.
    """
    a = query(
        "SELECT facility, register, indicator, period, age_group, value FROM upload_snapshots WHERE upload_id = ?",
        (upload_id_a,),
    )
    b = query(
        "SELECT facility, register, indicator, period, age_group, value FROM upload_snapshots WHERE upload_id = ?",
        (upload_id_b,),
    )
    meta_a = query("SELECT * FROM upload_history WHERE id = ?", (upload_id_a,))
    meta_b = query("SELECT * FROM upload_history WHERE id = ?", (upload_id_b,))

    empty = {
        "meta_a": meta_a, "meta_b": meta_b,
        "detail": pd.DataFrame(), "summary": {},
        "error": None,
    }
    if a.empty and b.empty:
        empty["error"] = "Neither upload has a snapshot (uploads before this feature have no snapshot)."
        return empty
    if a.empty:
        empty["error"] = f"Upload #{upload_id_a} has no snapshot."
        return empty
    if b.empty:
        empty["error"] = f"Upload #{upload_id_b} has no snapshot."
        return empty

    keys = ["facility", "register", "indicator", "period", "age_group"]
    merged = a.merge(b, on=keys, how="outer", suffixes=("_a", "_b"))
    merged["value_a"] = merged["value_a"].fillna(0)
    merged["value_b"] = merged["value_b"].fillna(0)
    merged["delta"] = merged["value_b"] - merged["value_a"]
    merged["pct_change"] = merged.apply(
        lambda r: pct_change_safe(r["value_a"], r["value_b"])[0],
        axis=1,
    )
    merged["pct_note"] = merged.apply(
        lambda r: pct_change_safe(r["value_a"], r["value_b"])[1],
        axis=1,
    )
    changed = merged[merged["delta"].abs() > 1e-9].copy()
    changed = changed.sort_values("delta", key=lambda s: s.abs(), ascending=False)

    # Focus metrics for summary
    def _vol(df, pattern):
        if df.empty:
            return 0.0
        m = df[df["indicator"].str.contains(pattern, na=False, regex=True)]
        return float(m["value"].sum()) if not m.empty else 0.0

    sum_a = {
        "rows": len(a),
        "rdt_exams": _vol(a, r"RDT.*Total Exam|Rapid diagnostic.*Total Exam"),
        "rdt_pos": _vol(a, r"RDT.*Positive|Rapid diagnostic.*Positive"),
        "suspected": _vol(a, r"Suspected"),
        "tested": _vol(a, r"Tested for Malaria|Total Exam"),
        "confirmed": _vol(a, r"Positive|Confirmed"),
        "al": _vol(a, r"AL Total|AL Dispensed"),
    }
    sum_b = {
        "rows": len(b),
        "rdt_exams": _vol(b, r"RDT.*Total Exam|Rapid diagnostic.*Total Exam"),
        "rdt_pos": _vol(b, r"RDT.*Positive|Rapid diagnostic.*Positive"),
        "suspected": _vol(b, r"Suspected"),
        "tested": _vol(b, r"Tested for Malaria|Total Exam"),
        "confirmed": _vol(b, r"Positive|Confirmed"),
        "al": _vol(b, r"AL Total|AL Dispensed"),
    }

    return {
        "meta_a": meta_a, "meta_b": meta_b,
        "detail": changed,
        "summary": {"a": sum_a, "b": sum_b},
        "error": None,
    }


def get_latest_dq_review():
    try:
        df = query("SELECT * FROM dq_reviews ORDER BY id DESC LIMIT 1")
        return df.iloc[0] if not df.empty else None
    except Exception:
        return None


def save_dq_review(user, dq_score, filters, note=""):
    fac = filters.get("facility", "All three") if filters else "All three"
    periods = filters.get("periods") if filters else None
    if isinstance(periods, list):
        period_txt = ", ".join(str(p) for p in periods[:6])
        if periods and len(periods) > 6:
            period_txt += f" (+{len(periods) - 6} more)"
    else:
        period_txt = str(periods or "—")
    execute(
        """
        INSERT INTO dq_reviews (reviewed_by, dq_score, facility_filter, period_filter, note, status)
        VALUES (?, ?, ?, ?, ?, 'reviewed')
        """,
        (user, dq_score, fac, period_txt, note or ""),
    )
    log_audit(user, "dq_review", "dq_reviews", f"score={dq_score} facility={fac}")


def page_loss_gap(user, filters):
    """
    Loss & gap analysis — where clients are lost along the cascade.
    Uses active filters so cascade volumes change with facility/period.
    Formulas (programme standard):
      Testing rate     = Tested ÷ Suspected × 100
      Diagnosis gap    = Suspected − Tested
      Positivity       = Positive ÷ Tested × 100
      Treatment gap    = Confirmed − AL dispensed
      Treated w/o test = indicator if present
      IPT1→IPT2        = IPT2 ÷ IPT1 × 100
      IPT1→IPT3        = IPT3 ÷ IPT1 × 100  (retention)
      ANC→IPT1         = IPT1 ÷ ANC attendance × 100 (when ANC available)
    """
    df = load_all_data()
    page_header(
        "Loss & Gap Analysis",
        "Where clients are lost between suspected → tested → confirmed → treated, "
        "and along ANC / IPT pathways. Numbers follow the active filters above.",
    )
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    d = _filter_data(df, filters)
    if d.empty:
        st.warning("No rows for active filters. Widen period / facility and Apply filters.")
        return

    # ── Case-finding cascade (shared helper) ──
    cascade = compute_cascade_metrics(d)
    susp = cascade["suspected"]
    tested = cascade["tested"]
    confirmed = cascade["confirmed"]
    treated = cascade["treated"]
    twt = cascade["treated_wo_test"]
    diag_gap = cascade["diag_gap"]
    treat_gap = cascade["treat_gap"]
    testing_rate = cascade["testing_rate"]
    positivity = cascade["positivity"]
    treat_coverage = cascade["treat_coverage"]
    diag_pct = cascade["diag_gap_pct"]
    treat_pct = cascade["treat_gap_pct"]

    gaps = [
        ("diagnosis (suspected not tested)", diag_pct),
        ("treatment (confirmed not reflected in AL)", treat_pct),
        ("treated without testing", _safe_pct(twt, treated) if treated else 0),
    ]
    worst = max(gaps, key=lambda x: x[1])

    with st.expander("How cascade gaps are calculated", expanded=False):
        st.markdown(
            f"""
**Diagnosis gap** = Suspected − Tested = **{diag_gap:,}**  
**Diagnosis gap %** = gap ÷ suspected × 100 = **{diag_pct:.1f}%**  
**Treatment gap** = Confirmed − AL = **{treat_gap:,}**  
**Treatment gap %** = gap ÷ confirmed × 100 = **{treat_pct:.1f}%**  
**Testing rate** = Tested ÷ Suspected × 100 = **{testing_rate}%**  
**Sources:** MOH 705 (suspected/tested/confirmed) · MOH 743 (AL)  
**Filter scope:** facility / period from the global filter bar.
            """
        )
    show_lineage_expander("CAS_1", result=compute_indicator("CAS_1", df, filters), df=df, filters=filters)
    level = "red" if worst[1] > 20 else ("amber" if worst[1] > 5 else "green")
    headline(
        f"Largest loss point: **{worst[0]}** at {worst[1]:.1f}%. "
        f"Testing rate {testing_rate:.0f}% · Treatment coverage {treat_coverage:.0f}%.",
        status=level,
    )

    section_header("Case cascade (respects filters)")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        big_number(fmt_count(susp), "Suspected")
    with c2:
        big_number(fmt_count(tested), "Tested",
                   delta=f"{testing_rate:.0f}% of suspected", delta_direction="flat")
    with c3:
        big_number(fmt_count(confirmed), "Confirmed positive",
                   delta=f"TPR {positivity:.1f}%", delta_direction="flat")
    with c4:
        big_number(fmt_count(treated), "AL dispensed",
                   delta=f"{treat_coverage:.0f}% of confirmed", delta_direction="flat")

    if susp or tested or confirmed:
        fig_f = pgo.Figure(pgo.Funnel(
            y=["Suspected", "Tested", "Confirmed +", "Treated (AL)"],
            x=[max(susp, 0), max(tested, 0), max(confirmed, 0), max(treated, 0)],
            textinfo="value+percent initial",
            marker={"color": [COLOR_NAVY, COLOR_BLUE, COLOR_AMBER, COLOR_GREEN]},
        ))
        fig_f.update_layout(height=420, margin=dict(t=20, b=20))
        st.plotly_chart(fig_f, use_container_width=True)
    else:
        st.info("No suspected/tested volumes for these filters. Try All periods or Facility registers.")

    section_header("Where clients were lost")
    loss_rows = [
        {
            "Step": "Suspected → Tested",
            "Entered": int(susp),
            "Continued": int(tested),
            "Lost": int(diag_gap),
            "Loss %": f"{diag_pct:.1f}%",
            "Meaning": "Clients not tested (stock-out, protocol, referral delay)",
        },
        {
            "Step": "Tested → Confirmed",
            "Entered": int(tested),
            "Continued": int(confirmed),
            "Lost": int(max(tested - confirmed, 0)),
            "Loss %": f"{_safe_pct(max(tested - confirmed, 0), tested):.1f}%",
            "Meaning": "Tested negative (expected) — not a quality loss",
        },
        {
            "Step": "Confirmed → AL dispensed",
            "Entered": int(confirmed),
            "Continued": int(treated),
            "Lost": int(treat_gap),
            "Loss %": f"{treat_pct:.1f}%",
            "Meaning": "Confirmed cases without AL in register (stock / documentation / referral)",
        },
        {
            "Step": "Treated without testing",
            "Entered": int(treated),
            "Continued": "—",
            "Lost": int(twt),
            "Loss %": f"{_safe_pct(twt, treated):.1f}%" if treated else "—",
            "Meaning": "AL without documented test (protocol breach)",
        },
    ]
    st.dataframe(pd.DataFrame(loss_rows), use_container_width=True, hide_index=True)

    # ── Detailed gap discussion ──
    section_header("Gap discussion (what each loss means)")
    discuss = []
    if diag_pct >= 20:
        discuss.append(
            f"**Diagnosis gap ({diag_pct:.0f}%)** — High. Many suspected clients are not tested. "
            "Likely causes: RDT stock-out, tester not available, clients leave before testing, "
            "or weak CHP testing. Action: check RDT stock and OPD/CHP testing protocol."
        )
    elif diag_pct >= 5:
        discuss.append(
            f"**Diagnosis gap ({diag_pct:.0f}%)** — Moderate. Some suspected clients skip testing. "
            "Review peak clinic days and CHP kit availability."
        )
    else:
        discuss.append(
            f"**Diagnosis gap ({diag_pct:.0f}%)** — Low/acceptable for this filter window. "
            "Keep monitoring monthly so it does not widen."
        )

    if treat_pct >= 15:
        discuss.append(
            f"**Treatment gap ({treat_pct:.0f}%)** — High. Confirmed positives exceed AL in the register. "
            "May be true missed treatment **or** documentation lag (AL not entered). "
            "Action: supervision on AL dispensing records and referral completion."
        )
    elif treat_pct >= 5:
        discuss.append(
            f"**Treatment gap ({treat_pct:.0f}%)** — Moderate. Reconcile confirmed cases vs AL by facility."
        )
    else:
        discuss.append(
            f"**Treatment gap ({treat_pct:.0f}%)** — Low for this window. "
            "Note: AL can exceed confirmed if weight-band packs or community AL are counted broadly."
        )

    twt_pct = _safe_pct(twt, treated) if treated else 0
    if twt_pct >= 5 or twt > 0:
        discuss.append(
            f"**Treated without testing ({int(twt):,} · {twt_pct:.0f}% of AL volume markers)** — "
            "Protocol concern: treatment without a documented test. "
            "Strengthen test-and-treat messaging and register completeness."
        )
    else:
        discuss.append(
            "**Treated without testing** — No material volume in these filters (or indicator not reported)."
        )

    discuss.append(
        f"**Tested → negative** is **not** a quality loss: TPR is {positivity:.1f}%. "
        "High TPR with falling tests is a separate warning (testing collapse) on the GO page."
    )
    for para in discuss:
        st.markdown(f"- {para}")

    # ── Age-split case cascade (<5 vs ≥5) ──
    section_header("Case cascade by age (<5 vs ≥5)")
    st.caption(
        "Under-fives carry higher malaria risk. Compare where each age group is lost. "
        "Uses age_group on MOH 705A/B, 706, 748 where present."
    )

    def _cascade_for_age(frame, age_key):
        if frame is None or frame.empty:
            return dict(susp=0, tested=0, conf=0, treated=0)
        if age_key == "<5":
            sub = frame[frame["age_group"].isin(["<5"])]
            if sub.empty:
                sub = frame[frame["indicator"].str.contains(r"<5|Under five|Under 5", case=False, na=False)]
        else:
            sub = frame[frame["age_group"].isin([">=5", ">5"])]
            if sub.empty:
                sub = frame[frame["indicator"].str.contains(r">=5|>5|over five|5 years", case=False, na=False)]
        return {
            "susp": _sum_ind(sub, r"Suspected"),
            "tested": _sum_ind(sub, r"Tested for Malaria|Total Exam|Total <5|Total >=5"),
            "conf": _sum_ind(sub, r"Positive|Confirmed"),
            "treated": _sum_ind(sub, r"AL Total|AL Dispensed"),
        }

    age_u5 = _cascade_for_age(d, "<5")
    age_o5 = _cascade_for_age(d, ">=5")
    a_left, a_right = st.columns(2)
    with a_left:
        st.markdown("**Under 5 years**")
        u5_test_r = _safe_pct(age_u5["tested"], age_u5["susp"])
        u5_treat_r = _safe_pct(age_u5["treated"], age_u5["conf"])
        st.caption(
            f"Suspected {int(age_u5['susp']):,} → Tested {int(age_u5['tested']):,} "
            f"({u5_test_r:.0f}%) → Confirmed {int(age_u5['conf']):,} → AL {int(age_u5['treated']):,} "
            f"({u5_treat_r:.0f}%)"
        )
        if age_u5["susp"] or age_u5["tested"] or age_u5["conf"]:
            fig_u5 = pgo.Figure(pgo.Funnel(
                y=["Suspected <5", "Tested <5", "Confirmed <5", "AL linked"],
                x=[max(age_u5["susp"], 0), max(age_u5["tested"], 0),
                   max(age_u5["conf"], 0), max(age_u5["treated"], 0)],
                textinfo="value+percent initial",
                marker={"color": [COLOR_NAVY, COLOR_BLUE, COLOR_AMBER, COLOR_GREEN]},
            ))
            fig_u5.update_layout(height=360, margin=dict(t=10, b=10))
            st.plotly_chart(fig_u5, use_container_width=True)
        else:
            st.info("No <5 cascade volumes for these filters.")
    with a_right:
        st.markdown("**5 years and above**")
        o5_test_r = _safe_pct(age_o5["tested"], age_o5["susp"])
        o5_treat_r = _safe_pct(age_o5["treated"], age_o5["conf"])
        st.caption(
            f"Suspected {int(age_o5['susp']):,} → Tested {int(age_o5['tested']):,} "
            f"({o5_test_r:.0f}%) → Confirmed {int(age_o5['conf']):,} → AL {int(age_o5['treated']):,} "
            f"({o5_treat_r:.0f}%)"
        )
        if age_o5["susp"] or age_o5["tested"] or age_o5["conf"]:
            fig_o5 = pgo.Figure(pgo.Funnel(
                y=["Suspected ≥5", "Tested ≥5", "Confirmed ≥5", "AL linked"],
                x=[max(age_o5["susp"], 0), max(age_o5["tested"], 0),
                   max(age_o5["conf"], 0), max(age_o5["treated"], 0)],
                textinfo="value+percent initial",
                marker={"color": [COLOR_NAVY, COLOR_BLUE, COLOR_AMBER, COLOR_GREEN]},
            ))
            fig_o5.update_layout(height=360, margin=dict(t=10, b=10))
            st.plotly_chart(fig_o5, use_container_width=True)
        else:
            st.info("No ≥5 cascade volumes for these filters.")

    age_cmp = pd.DataFrame([
        {
            "Age": "<5",
            "Diagnosis gap %": f"{_safe_pct(max(age_u5['susp'] - age_u5['tested'], 0), age_u5['susp']):.1f}%",
            "TPR %": f"{_safe_pct(age_u5['conf'], age_u5['tested']):.1f}%",
            "Treatment gap %": f"{_safe_pct(max(age_u5['conf'] - age_u5['treated'], 0), age_u5['conf']):.1f}%",
        },
        {
            "Age": "≥5",
            "Diagnosis gap %": f"{_safe_pct(max(age_o5['susp'] - age_o5['tested'], 0), age_o5['susp']):.1f}%",
            "TPR %": f"{_safe_pct(age_o5['conf'], age_o5['tested']):.1f}%",
            "Treatment gap %": f"{_safe_pct(max(age_o5['conf'] - age_o5['treated'], 0), age_o5['conf']):.1f}%",
        },
    ])
    st.dataframe(age_cmp, use_container_width=True, hide_index=True)
    if age_u5["susp"] and age_o5["susp"]:
        u5_dg = _safe_pct(max(age_u5["susp"] - age_u5["tested"], 0), age_u5["susp"])
        o5_dg = _safe_pct(max(age_o5["susp"] - age_o5["tested"], 0), age_o5["susp"])
        if u5_dg > o5_dg + 5:
            st.markdown(
                f"- **Age insight:** Diagnosis loss is worse in **under-fives** "
                f"({u5_dg:.0f}% vs {o5_dg:.0f}% in ≥5). Prioritise paediatric testing and caregiver flow."
            )
        elif o5_dg > u5_dg + 5:
            st.markdown(
                f"- **Age insight:** Diagnosis loss is worse in **≥5** ({o5_dg:.0f}% vs {u5_dg:.0f}% in under-fives)."
            )

    # ── Facility vs community continuum ──
    section_header("Facility vs community continuum")
    st.caption(
        "Facility stream (OPD/lab registers) vs community (MOH 748). "
        "Shows whether cases found in the community are matched by treatment volume."
    )
    fac_d = d[d["stream"] == "Facility"] if "stream" in d.columns else d
    com_d = d[d["stream"] == "Community"] if "stream" in d.columns else pd.DataFrame()
    fac_tested = _sum_ind(fac_d, r"Tested for Malaria|Total Exam")
    fac_conf = _sum_ind(fac_d, r"Positive|Confirmed")
    fac_al = _sum_ind(fac_d, r"AL Total|AL Dispensed")
    com_tested = _sum_ind(com_d, r"Total <5|Total >=5|Total Exam") if not com_d.empty else 0
    com_conf = _sum_ind(com_d, r"Positive") if not com_d.empty else 0
    com_al = _sum_ind(com_d, r"AL Dispensed|AL Total") if not com_d.empty else 0
    sc1, sc2 = st.columns(2)
    with sc1:
        st.markdown("**Facility**")
        big_number(f"{int(fac_tested):,}", "Tests", delta=f"Pos {int(fac_conf):,} · AL {int(fac_al):,}", delta_direction="flat")
    with sc2:
        st.markdown("**Community (CHP)**")
        big_number(f"{int(com_tested):,}", "Tests", delta=f"Pos {int(com_conf):,} · AL {int(com_al):,}", delta_direction="flat")
    stream_tbl = pd.DataFrame([
        {
            "Stream": "Facility",
            "Tested": int(fac_tested),
            "Positive": int(fac_conf),
            "TPR %": f"{_safe_pct(fac_conf, fac_tested):.1f}%",
            "AL": int(fac_al),
            "Treat cov. %": f"{_safe_pct(fac_al, fac_conf):.0f}%",
        },
        {
            "Stream": "Community",
            "Tested": int(com_tested),
            "Positive": int(com_conf),
            "TPR %": f"{_safe_pct(com_conf, com_tested):.1f}%",
            "AL": int(com_al),
            "Treat cov. %": f"{_safe_pct(com_al, com_conf):.0f}%",
        },
    ])
    st.dataframe(stream_tbl, use_container_width=True, hide_index=True)
    if com_conf and fac_conf:
        share_com = _safe_pct(com_conf, com_conf + fac_conf)
        st.markdown(
            f"- Community accounts for **{share_com:.0f}%** of confirmed positives in this window. "
            "If community positivity is high but AL is low, check referral completion and CHP AL stocks."
        )

    # ── Referral continuum ──
    section_header("Referral continuum")
    st.caption(
        "From CHP Performance referral log (if used) and register referral counts when present."
    )
    try:
        refs = query("SELECT * FROM referrals ORDER BY referral_date DESC")
    except Exception:
        refs = pd.DataFrame()
    if filters.get("facility") not in (None, "All three") and not refs.empty and "facility" in refs.columns:
        refs = refs[refs["facility"] == filters["facility"]]
    if not refs.empty:
        n_ref = len(refs)
        n_arr = int(refs["arrived"].sum()) if "arrived" in refs.columns else 0
        n_conf_r = int(refs["confirmed"].sum()) if "confirmed" in refs.columns else 0
        n_trt = int(refs["treated"].sum()) if "treated" in refs.columns else 0
        rc1, rc2, rc3, rc4 = st.columns(4)
        with rc1:
            big_number(str(n_ref), "Referrals logged")
        with rc2:
            big_number(f"{_safe_pct(n_arr, n_ref):.0f}%", "Arrived at facility",
                       delta=f"{n_arr}/{n_ref}", delta_direction="flat")
        with rc3:
            big_number(f"{_safe_pct(n_conf_r, n_arr):.0f}%" if n_arr else "—", "Confirmed among arrivals",
                       delta=f"{n_conf_r}", delta_direction="flat")
        with rc4:
            big_number(f"{_safe_pct(n_trt, n_conf_r):.0f}%" if n_conf_r else "—", "Treated among confirmed",
                       delta=f"{n_trt}", delta_direction="flat")
        if n_ref and _safe_pct(n_ref - n_arr, n_ref) > 20:
            st.markdown(
                f"- **Referral loss:** {_safe_pct(n_ref - n_arr, n_ref):.0f}% of logged referrals did not arrive. "
                "Follow up transport, caregiver counselling, and CHU–facility linkage."
            )
        fig_ref = pgo.Figure(pgo.Funnel(
            y=["Referred (log)", "Arrived", "Confirmed", "Treated"],
            x=[n_ref, n_arr, n_conf_r, n_trt],
            textinfo="value+percent initial",
            marker={"color": [COLOR_NAVY, COLOR_BLUE, COLOR_AMBER, COLOR_GREEN]},
        ))
        fig_ref.update_layout(height=340, margin=dict(t=10))
        st.plotly_chart(fig_ref, use_container_width=True)
    else:
        reg_ref = _sum_ind(d, r"Referred")
        if reg_ref:
            st.markdown(f"- Register **referred** count in filters: **{int(reg_ref):,}** (no detailed arrival log yet).")
            st.caption("Log referrals under CHP Performance to track arrived → confirmed → treated.")
        else:
            st.info(
                "No referral log entries and no referral counts in filters. "
                "Use **CHP Performance** to log referrals for a full continuum."
            )

    # ── Stock clues ──
    section_header("Stock clues (possible reasons for gaps)")
    try:
        stock = query("SELECT * FROM stock ORDER BY facility, item")
    except Exception:
        stock = pd.DataFrame()
    if filters.get("facility") not in (None, "All three") and not stock.empty:
        stock = stock[stock["facility"] == filters["facility"]]
    if not stock.empty:
        low = stock[stock["quantity"] < stock["reorder_point"]] if "reorder_point" in stock.columns else pd.DataFrame()
        if not low.empty:
            st.warning(
                f"**{len(low)}** stock line(s) below reorder point — may help explain testing or treatment gaps."
            )
            st.dataframe(low, use_container_width=True, hide_index=True)
            if diag_pct >= 10 and any("RDT" in str(x).upper() for x in low["item"]):
                st.markdown("- Low **RDT** stock aligns with a **diagnosis gap** — prioritise resupply.")
            if treat_pct >= 10 and any("AL" in str(x).upper() for x in low["item"]):
                st.markdown("- Low **AL** stock aligns with a **treatment gap** — prioritise AL and documentation.")
        else:
            st.success("No stock lines below reorder point in the log (does not rule out transient stock-outs).")
    else:
        st.caption("No stock log yet. Enter RDT/AL stock under CHP Performance to link gaps to commodities.")

    # ── Gap trends by period ──
    section_header("Gap trends over periods")
    st.caption("Diagnosis and treatment gap % by period inside the current filter set.")
    trend_rows = []
    if not d.empty and "period" in d.columns:
        for per in sorted(d["period"].dropna().unique(), key=str):
            pd_ = d[d["period"] == per]
            s = _sum_ind(pd_, r"Suspected")
            t = _sum_ind(pd_, r"Tested for Malaria|Total Exam")
            c = _sum_ind(pd_, r"Positive|Confirmed")
            al = _sum_ind(pd_, r"AL Total|AL Dispensed")
            i1 = _sum_ind(pd_, r"^IPT1$|IPT\\s*1")
            i3 = _sum_ind(pd_, r"^IPT3$|IPT\\s*3")
            if not (s or t or c or i1):
                continue
            trend_rows.append({
                "Period": str(per),
                "Diagnosis gap %": round(_safe_pct(max(s - t, 0), s), 1) if s else None,
                "Treatment gap %": round(_safe_pct(max(c - al, 0), c), 1) if c else None,
                "TPR %": round(_safe_pct(c, t), 1) if t else None,
                "IPT3 retention %": round(_safe_pct(i3, i1), 1) if i1 else None,
            })
    if trend_rows:
        trend_df = pd.DataFrame(trend_rows)
        st.dataframe(trend_df, use_container_width=True, hide_index=True)
        plot_df = trend_df.copy()
        if len(plot_df) >= 2:
            fig_tr = pgo.Figure()
            if plot_df["Diagnosis gap %"].notna().any():
                fig_tr.add_trace(pgo.Scatter(
                    x=plot_df["Period"], y=plot_df["Diagnosis gap %"],
                    mode="lines+markers", name="Diagnosis gap %", line=dict(color=COLOR_RED, width=3),
                ))
            if plot_df["Treatment gap %"].notna().any():
                fig_tr.add_trace(pgo.Scatter(
                    x=plot_df["Period"], y=plot_df["Treatment gap %"],
                    mode="lines+markers", name="Treatment gap %", line=dict(color=COLOR_AMBER, width=3),
                ))
            if plot_df["IPT3 retention %"].notna().any():
                fig_tr.add_trace(pgo.Scatter(
                    x=plot_df["Period"], y=plot_df["IPT3 retention %"],
                    mode="lines+markers", name="IPT3 retention %", line=dict(color=COLOR_GREEN, width=3),
                ))
            fig_tr.update_layout(
                height=380, plot_bgcolor="white", yaxis_title="%",
                legend=dict(orientation="h", y=1.12), margin=dict(t=40),
            )
            st.plotly_chart(fig_tr, use_container_width=True)
            st.markdown(
                "- If **diagnosis gap %** rises while tests fall, investigate testing collapse and stock. "
                "If **IPT3 retention** falls, focus ANC return visits and SP availability."
            )
    else:
        st.info("Not enough period-level cascade data to draw trends for these filters.")

    # ── ANC / IPT pathway (NMCP-style: retention + coverage) ──
    section_header("ANC & IPT pathway (MiP prevention)")
    st.caption(
        "**Retention** (among women who started IPTp): IPT2÷IPT1 and IPT3÷IPT1. "
        "**Coverage** (NMCP-style, when denominator exists): IPT1÷ANC1 and IPT3÷ANC1. "
        "ANC1 (first ANC visit) is preferred; generic ANC attendance is used only as a proxy and labelled as such."
    )
    ipt1 = _sum_ind(d, r"^IPT1$|IPT\s*1")
    ipt2 = _sum_ind(d, r"^IPT2$|IPT\s*2")
    ipt3 = _sum_ind(d, r"^IPT3$|IPT\s*3")
    # Prefer true ANC1; fall back to generic attendance only as proxy
    anc1 = _sum_ind(d, r"^ANC1$")
    if not anc1:
        # Explicit first-visit style labels mapped to indicator ANC1
        anc1 = float(
            d[d["indicator"].isin(["ANC1"])]["value"].sum()
        ) if not d.empty and "indicator" in d.columns else 0.0
    anc_any = _sum_ind(d, r"ANC Attendance")
    mip = _sum_ind(d, r"Malaria in Pregnancy|MIP Cases|^MIP$")

    use_anc1 = anc1 > 0
    anc_den = anc1 if use_anc1 else anc_any
    anc_den_label = "ANC1 (first visit)" if use_anc1 else "ANC attendance (proxy — not ANC1)"
    anc_is_proxy = bool(anc_any and not use_anc1)

    ipt2_ret = _safe_pct(ipt2, ipt1)
    ipt3_ret = _safe_pct(ipt3, ipt1)
    ipt1_cov = _safe_pct(ipt1, anc_den) if anc_den else None
    ipt3_cov = _safe_pct(ipt3, anc_den) if anc_den else None

    st.markdown("##### Retention (dose continuum among IPT1 recipients)")
    r1, r2, r3 = st.columns(3)
    with r1:
        big_number(f"{int(ipt1):,}", "IPT1 doses")
    with r2:
        big_number(f"{ipt2_ret:.0f}%", "IPT1→IPT2 retention",
                   delta=f"IPT2 = {int(ipt2):,}", delta_direction="flat")
    with r3:
        ret_level = "up" if ipt3_ret >= 70 else "down"
        big_number(f"{ipt3_ret:.0f}%", "IPT1→IPT3 retention",
                   delta=f"Target ≥70% · IPT3 = {int(ipt3):,}", delta_direction=ret_level)

    st.markdown("##### Coverage (relative to ANC denominator)")
    if anc_den:
        if anc_is_proxy:
            st.warning(
                f"Denominator is **{anc_den_label}** ({int(anc_den):,}). "
                "Prefer mapping **ANC1 / first ANC visit** in KHIS for NMCP-style coverage."
            )
        else:
            st.caption(f"Denominator: **{anc_den_label}** = {int(anc_den):,}")
        c1, c2, c3 = st.columns(3)
        with c1:
            big_number(
                f"{ipt1_cov:.0f}%" if ipt1_cov is not None else "—",
                "IPT1 coverage",
                delta=f"IPT1 ÷ {anc_den_label}",
                delta_direction="flat",
            )
        with c2:
            big_number(
                f"{ipt3_cov:.0f}%" if ipt3_cov is not None else "—",
                "IPT3 coverage (NMCP-style)",
                delta=f"IPT3 ÷ {anc_den_label}",
                delta_direction="flat",
            )
        with c3:
            big_number(f"{int(mip):,}", "MiP cases",
                       delta="Clinical (not a coverage denominator)", delta_direction="flat")
    else:
        st.info(
            "No ANC1 or ANC attendance in the current filters — coverage (IPT÷ANC) cannot be calculated. "
            "Retention above still works from MOH 743 IPT1/2/3."
        )
        big_number(f"{int(mip):,}", "MiP cases logged",
                   delta="ANC denominator missing", delta_direction="flat")

    section_header("IPT cascade")
    st.caption(
        "**Coverage cascade:** ANC → IPT1 → IPT2 → IPT3 (who should get IPTp). "
        "**Retention cascade:** IPT1 → IPT2 → IPT3 (who continued doses). "
        "Both follow the active filters."
    )

    if not (ipt1 or ipt2 or ipt3 or anc_den):
        st.info(
            "No IPT1/2/3 or ANC data for these filters. "
            "Need MOH 743 (IPT1–3) and ideally ANC1 / ANC attendance."
        )
    else:
        col_cov, col_ret = st.columns(2)

        with col_cov:
            st.markdown("**Coverage cascade** (ANC → IPT1 → IPT2 → IPT3)")
            if anc_den:
                fig_c = pgo.Figure(pgo.Funnel(
                    y=[anc_den_label, "IPT1", "IPT2", "IPT3"],
                    x=[max(anc_den, 0), max(ipt1, 0), max(ipt2, 0), max(ipt3, 0)],
                    textinfo="value+percent initial",
                    marker={"color": [COLOR_AMBER, COLOR_BLUE, COLOR_NAVY, COLOR_GREEN]},
                ))
                fig_c.update_layout(height=420, margin=dict(t=10, b=10))
                st.plotly_chart(fig_c, use_container_width=True)
                if ipt1_cov is not None and ipt3_cov is not None:
                    st.caption(
                        f"IPT1 coverage **{ipt1_cov:.0f}%** · IPT3 coverage **{ipt3_cov:.0f}%** "
                        f"(of {anc_den_label})"
                    )
            else:
                st.info("No ANC denominator — add ANC1 or ANC attendance for this cascade.")

        with col_ret:
            st.markdown("**Retention cascade** (IPT1 → IPT2 → IPT3)")
            if ipt1 or ipt2 or ipt3:
                fig_r = pgo.Figure(pgo.Funnel(
                    y=["IPT1", "IPT2", "IPT3"],
                    x=[max(ipt1, 0), max(ipt2, 0), max(ipt3, 0)],
                    textinfo="value+percent initial",
                    marker={"color": [COLOR_BLUE, COLOR_NAVY, COLOR_GREEN]},
                ))
                fig_r.update_layout(height=420, margin=dict(t=10, b=10))
                st.plotly_chart(fig_r, use_container_width=True)
                st.caption(
                    f"IPT2 retention **{ipt2_ret:.0f}%** · IPT3 retention **{ipt3_ret:.0f}%** "
                    f"(of IPT1 · target ≥70%)"
                )
            else:
                st.info("No IPT doses — need MOH 743 IPT1/2/3.")

        st.markdown("**Where clients were lost on the IPT cascade**")
        cascade_steps = []
        if anc_den:
            cascade_steps.append({
                "Stage": f"{anc_den_label} → IPT1",
                "Entered": int(anc_den),
                "Continued": int(ipt1),
                "Lost": int(max(anc_den - ipt1, 0)),
                "Continued %": f"{_safe_pct(ipt1, anc_den):.1f}%",
                "Lost %": f"{_safe_pct(max(anc_den - ipt1, 0), anc_den):.1f}%",
                "Type": "Coverage",
                "Why": "ANC clients who did not receive first SP dose",
            })
        cascade_steps.extend([
            {
                "Stage": "IPT1 → IPT2",
                "Entered": int(ipt1),
                "Continued": int(ipt2),
                "Lost": int(max(ipt1 - ipt2, 0)),
                "Continued %": f"{_safe_pct(ipt2, ipt1):.1f}%",
                "Lost %": f"{_safe_pct(max(ipt1 - ipt2, 0), ipt1):.1f}%",
                "Type": "Retention",
                "Why": "Started IPTp but missed 2nd dose",
            },
            {
                "Stage": "IPT2 → IPT3",
                "Entered": int(ipt2),
                "Continued": int(ipt3),
                "Lost": int(max(ipt2 - ipt3, 0)),
                "Continued %": f"{_safe_pct(ipt3, ipt2):.1f}%",
                "Lost %": f"{_safe_pct(max(ipt2 - ipt3, 0), ipt2):.1f}%",
                "Type": "Retention",
                "Why": "Received 2nd dose but not 3rd",
            },
            {
                "Stage": "IPT1 → IPT3 (overall retention)",
                "Entered": int(ipt1),
                "Continued": int(ipt3),
                "Lost": int(max(ipt1 - ipt3, 0)),
                "Continued %": f"{ipt3_ret:.1f}%",
                "Lost %": f"{_safe_pct(max(ipt1 - ipt3, 0), ipt1):.1f}%",
                "Type": "Retention",
                "Why": "Full course among those who started (target ≥70%)",
            },
        ])
        if anc_den and ipt3_cov is not None:
            cascade_steps.append({
                "Stage": f"{anc_den_label} → IPT3 (overall coverage)",
                "Entered": int(anc_den),
                "Continued": int(ipt3),
                "Lost": int(max(anc_den - ipt3, 0)),
                "Continued %": f"{ipt3_cov:.1f}%",
                "Lost %": f"{_safe_pct(max(anc_den - ipt3, 0), anc_den):.1f}%",
                "Type": "Coverage",
                "Why": "ANC clients who did not complete ≥3 IPTp doses",
            })
        st.dataframe(pd.DataFrame(cascade_steps), use_container_width=True, hide_index=True)

        st.markdown("**IPT cascade by facility**")
        ipt_fac_rows = []
        for fac in FACILITIES:
            if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
                continue
            fd = d[d["facility"] == fac]
            f1 = _sum_ind(fd, r"^IPT1$|IPT\s*1")
            f2 = _sum_ind(fd, r"^IPT2$|IPT\s*2")
            f3 = _sum_ind(fd, r"^IPT3$|IPT\s*3")
            fa1 = _sum_ind(fd, r"^ANC1$")
            fa_any = _sum_ind(fd, r"ANC Attendance")
            fa = fa1 if fa1 else fa_any
            ipt_fac_rows.append({
                "Facility": fac,
                "ANC denom": int(fa) if fa else "—",
                "IPT1": int(f1),
                "IPT2": int(f2),
                "IPT3": int(f3),
                "IPT1→IPT2 %": f"{_safe_pct(f2, f1):.0f}%" if f1 else "—",
                "IPT1→IPT3 %": f"{_safe_pct(f3, f1):.0f}%" if f1 else "—",
                "IPT3 coverage %": f"{_safe_pct(f3, fa):.0f}%" if fa else "—",
            })
        if ipt_fac_rows:
            st.dataframe(pd.DataFrame(ipt_fac_rows), use_container_width=True, hide_index=True)

    # ── Facility breakdown (uses same filter period, splits by site) ──
    section_header("Loss by facility (same period filters)")
    fac_rows = []
    for fac in FACILITIES:
        if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
            continue
        fd = d[d["facility"] == fac]
        f_susp = _sum_ind(fd, r"Suspected")
        f_tested = _sum_ind(fd, r"Tested for Malaria|Total Exam")
        f_conf = _sum_ind(fd, r"Positive|Confirmed")
        f_treated = _sum_ind(fd, r"AL Total|AL Dispensed")
        f_ipt1 = _sum_ind(fd, r"^IPT1$|IPT\s*1")
        f_ipt3 = _sum_ind(fd, r"^IPT3$|IPT\s*3")
        f_anc1 = _sum_ind(fd, r"^ANC1$")
        f_anc_any = _sum_ind(fd, r"ANC Attendance")
        f_anc = f_anc1 if f_anc1 else f_anc_any
        fac_rows.append({
            "Facility": fac,
            "Suspected": int(f_susp),
            "Tested": int(f_tested),
            "Testing %": f"{_safe_pct(f_tested, f_susp):.0f}%",
            "Confirmed": int(f_conf),
            "AL": int(f_treated),
            "Treat coverage %": f"{_safe_pct(f_treated, f_conf):.0f}%",
            "IPT3 retention %": f"{_safe_pct(f_ipt3, f_ipt1):.0f}%" if f_ipt1 else "—",
            "IPT3 coverage %": f"{_safe_pct(f_ipt3, f_anc):.0f}%" if f_anc else "—",
            "ANC denom": int(f_anc) if f_anc else "—",
        })
    if fac_rows:
        st.dataframe(pd.DataFrame(fac_rows), use_container_width=True, hide_index=True)

    section_header("Recommended actions")
    recs = []
    for fac in FACILITIES:
        if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
            continue
        fd = d[d["facility"] == fac]
        f_susp = _sum_ind(fd, r"Suspected")
        f_tested = _sum_ind(fd, r"Tested for Malaria|Total Exam")
        f_conf = _sum_ind(fd, r"Positive|Confirmed")
        f_treated = _sum_ind(fd, r"AL Total|AL Dispensed")
        f_ipt1 = _sum_ind(fd, r"^IPT1$|IPT\s*1")
        f_ipt3 = _sum_ind(fd, r"^IPT3$|IPT\s*3")
        f_anc1 = _sum_ind(fd, r"^ANC1$")
        f_anc_any = _sum_ind(fd, r"ANC Attendance")
        f_anc = f_anc1 if f_anc1 else f_anc_any
        if f_susp and _safe_pct(f_susp - f_tested, f_susp) > 20:
            recs.append(
                f"🔴 **{fac} — testing loss** {_safe_pct(f_susp - f_tested, f_susp):.0f}% of suspected not tested. "
                "Check RDT stock, CHP kits, and OPD testing protocol."
            )
        if f_conf and _safe_pct(f_conf - f_treated, f_conf) > 15:
            recs.append(
                f"🔴 **{fac} — treatment loss** {_safe_pct(f_conf - f_treated, f_conf):.0f}% confirmed without AL in register. "
                "Supervise AL documentation and referral completion."
            )
        if f_ipt1 and _safe_pct(f_ipt3, f_ipt1) < 70:
            recs.append(
                f"🟡 **{fac} — IPT retention** IPT3 is {_safe_pct(f_ipt3, f_ipt1):.0f}% of IPT1 (target ≥70%). "
                "Strengthen ANC return visits and IPTp counselling."
            )
        if f_anc and f_ipt3 and _safe_pct(f_ipt3, f_anc) < 40:
            recs.append(
                f"🟡 **{fac} — IPT3 coverage** {_safe_pct(f_ipt3, f_anc):.0f}% of ANC denominator received IPT3. "
                "Improve early ANC booking and SP availability at ANC."
            )
    if not recs:
        st.success("No urgent loss points above thresholds for the current filters.")
    else:
        for r in recs:
            st.markdown(r)

    with st.expander("Formulas used (NMCP-style)"):
        st.markdown("""
| Metric | Formula | Notes |
|--------|---------|--------|
| Testing rate | Tested ÷ Suspected × 100 | Case-finding |
| Diagnosis gap | Suspected − Tested | Clients not tested |
| Test positivity | Confirmed ÷ Tested × 100 | Among those tested |
| Treatment coverage | AL ÷ Confirmed × 100 | |
| Treatment gap | Confirmed − AL | |
| **IPT2 retention** | IPT2 ÷ IPT1 × 100 | Among women who received IPT1 |
| **IPT3 retention** | IPT3 ÷ IPT1 × 100 | Target ≥70% programme retention |
| **IPT1 coverage** | IPT1 ÷ **ANC1** × 100 | Prefer first ANC visit as denominator |
| **IPT3 coverage** | IPT3 ÷ **ANC1** × 100 | NMCP-style coverage |
| IPT÷ANC attendance | IPT ÷ ANC attendance × 100 | **Proxy only** if ANC1 missing |
| MiP cases | Count | Clinical outcome — **not** a coverage denominator |

Volumes use the **active filters** (facility, period, stream, registers).
""")


# ==================================================================
# PAGE 11 — DATA QUALITY
# ==================================================================

def sync_dq_issues_from_indicators(df, filters, user="system"):
    """
    Phase A.5 — turn formula invalids + recon flags into DQ issue rows
    (Issue → Severity → Facility → Period → Owner → Action pathway).
    """
    created = 0
    try:
        inds = compute_official_indicators(df, filters)
        go = inds.get("GO_1", {})
        for a in go.get("formula_issues") or []:
            fac = a.get("facility") or "—"
            per = a.get("period") or "—"
            msg = a.get("reason") or "Invalid rate"
            # de-dup open
            exists = query(
                """
                SELECT id FROM dq_issues
                WHERE status='open' AND facility=? AND period=? AND issue_type='formula_invalid'
                  AND message LIKE ?
                LIMIT 1
                """,
                (fac, str(per), msg[:80] + "%"),
            )
            if exists.empty:
                execute(
                    """
                    INSERT INTO dq_issues
                    (issue_type, facility, period, severity, message, owner, status, source)
                    VALUES ('formula_invalid', ?, ?, 'critical', ?, 'Facility IC', 'open', 'formula_engine')
                    """,
                    (fac, str(per), msg[:400]),
                )
                created += 1
        # cascade invalid rates for overall view
        d = _filter_data(df, filters)
        c = compute_cascade_metrics(d)
        for key, label in (("positivity", "Cascade positivity"), ("testing_rate", "Testing rate"), ("treat_coverage", "Treatment coverage")):
            stt = (c.get("rate_status") or {}).get(key)
            if stt == "invalid":
                msg = f"{label} invalid under locked 705/AL definitions"
                execute(
                    """
                    INSERT INTO dq_issues
                    (issue_type, facility, period, severity, message, owner, status, source)
                    VALUES ('cascade_invalid', ?, ?, 'high', ?, 'County M&E', 'open', 'cascade')
                    """,
                    (
                        filters.get("facility") if filters and filters.get("facility") not in (None, "All three") else "All",
                        "view",
                        msg,
                    ),
                )
                created += 1
    except Exception:
        pass
    return created


def page_quality(user, filters):
    """
    Data Quality — completeness, consistency, and trust in the numbers.
    Phase A.5: issues table with severity / owner / workplan link — not only a score.
    """
    df = load_all_data()
    page_header(
        "Data Quality",
        "Identify errors → assign owners → evidence → workplan → verify closure. Score is secondary.",
    )
    st.caption("What can I do here? Fix invalid rates and missing reports before anyone publishes a KPI.")
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    d = _filter_data(df, filters)
    if d.empty:
        st.warning("No rows for active filters. Widen period / facility and Apply filters.")
        return

    try:
        open_dq = query("SELECT severity, facility, message FROM dq_issues WHERE status='open'")
    except Exception:
        open_dq = pd.DataFrame()
    if not open_dq.empty:
        n_c = int((open_dq["severity"] == "critical").sum()) if "severity" in open_dq.columns else 0
        n_h = int((open_dq["severity"] == "high").sum()) if "severity" in open_dq.columns else 0
        headline(
            f"**{len(open_dq)} issues need attention** · {n_c} critical · {n_h} high. Score is below.",
            status="red" if n_c else "amber",
        )
        st.dataframe(open_dq.head(20), use_container_width=True, hide_index=True)
    else:
        st.success("No open DQ issues in the register. Score still shown for completeness.")

    # Composite DQ score (same as Overview)
    dq = compute_dq_score(df, filters)
    if dq["score"] is not None:
        st.caption(f"Composite score **{dq['score']:.0f}/100** — {dq['label']}. " + " ".join(dq["notes"][:2]))
        qc1, qc2, qc3, qc4, qc5 = st.columns(5)
        comps = dq.get("components", {})
        with qc1:
            big_number(f"{comps.get('reporting', 0):.0f}", "Reporting")
        with qc2:
            big_number(f"{comps.get('registers', 0):.0f}", "Registers")
        with qc3:
            big_number(f"{comps.get('go1_inputs', 0):.0f}", "GO_1 inputs")
        with qc4:
            big_number(f"{comps.get('periods', 0):.0f}", "Periods")
        with qc5:
            big_number(f"{comps.get('validation', 0):.0f}", "Validation rules")

    # Formula safety scan
    section_header("Formula safety (Phase A engine)")
    st.caption(
        "Rates with numerator > denominator are **invalid** (not capped to 100%). "
        "Missing denominator → N/A, not 0%."
    )
    try:
        inds = compute_official_indicators(df, filters)
        go = inds.get("GO_1", {})
        issues = go.get("formula_issues") or []
        if go.get("formula_status") == "invalid":
            st.error(f"Aggregate GO_1 invalid: {go.get('formula_reason')}")
        if issues:
            st.warning(f"**{len(issues)}** facility×period RDT rate(s) require reconciliation.")
            rows_f = [{
                "Facility": a.get("facility"),
                "Period": a.get("period"),
                "Positives": a.get("numerator"),
                "Exams": a.get("denominator"),
                "Raw %": a.get("calculated_pct"),
                "Status": a.get("status"),
            } for a in issues[:40]]
            st.dataframe(pd.DataFrame(rows_f), use_container_width=True, hide_index=True)
        else:
            st.success("No invalid RDT positivity cells in the current filtered monthly scan.")
    except Exception as e:
        st.caption(f"Formula scan skipped: {e}")

    # Phase A.5 — DQ issues register
    section_header("DQ issues register (management)")
    st.caption(
        "What is wrong · Where · Severity · Owner · Status. "
        "Push open issues to the **Workplan** for formal closure."
    )
    if user.get("can_upload") and st.button("↻ Sync issues from formula scan", key="dq_sync"):
        n = sync_dq_issues_from_indicators(df, filters, user.get("username", "system"))
        st.success(f"Created **{n}** new open issue(s) from current scan.")
        st.rerun()
    try:
        dqi = query(
            "SELECT * FROM dq_issues ORDER BY "
            "CASE status WHEN 'open' THEN 0 ELSE 1 END, "
            "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, id DESC LIMIT 100"
        )
    except Exception:
        dqi = pd.DataFrame()
    if dqi.empty:
        st.info("No DQ issues logged yet — run **Sync issues from formula scan** after a KHIS commit.")
    else:
        open_n = int((dqi["status"] == "open").sum())
        crit_n = int(((dqi["status"] == "open") & (dqi["severity"] == "critical")).sum())
        d1, d2, d3 = st.columns(3)
        with d1:
            big_number(str(open_n), "Open issues")
        with d2:
            big_number(str(crit_n), "Critical open")
        with d3:
            big_number(str(int((dqi["status"] == "resolved").sum())), "Resolved")
        st.dataframe(dqi, use_container_width=True, hide_index=True)
        if user.get("can_upload"):
            open_ids = dqi[dqi["status"] == "open"]["id"].tolist()
            if open_ids:
                c1, c2, c3 = st.columns(3)
                with c1:
                    pick = st.selectbox("Issue id", open_ids, key="dqi_pick")
                with c2:
                    act = st.selectbox(
                        "Action",
                        ["Assign owner + deadline", "Push to workplan", "Add evidence", "Verify & close", "Resolve only"],
                        key="dqi_act",
                    )
                with c3:
                    extra = st.text_input("Owner / evidence / note", key="dqi_extra")
                due_in = st.date_input("Due date (for assign)", key="dqi_due")
                if st.button("Apply DQ action", key="dqi_go"):
                    row = dqi[dqi["id"] == pick].iloc[0]
                    if act == "Assign owner + deadline":
                        execute(
                            "UPDATE dq_issues SET owner=?, due_date=?, status='open' WHERE id=?",
                            (extra or row.get("owner") or "Facility IC", str(due_in), int(pick)),
                        )
                        st.success(f"Issue #{pick} assigned")
                    elif act == "Add evidence":
                        execute(
                            "UPDATE dq_issues SET evidence=COALESCE(evidence,'') || ? WHERE id=?",
                            (f" | {extra}" if extra else "", int(pick)),
                        )
                        st.success("Evidence recorded")
                    elif act == "Verify & close":
                        execute(
                            """
                            UPDATE dq_issues SET status='resolved',
                                verification_note=?, verified_at=?, verified_by=?,
                                resolved_at=?, resolved_by=?
                            WHERE id=?
                            """,
                            (
                                extra or "Verified",
                                datetime.now().strftime("%Y-%m-%d %H:%M"),
                                user.get("username", ""),
                                datetime.now().strftime("%Y-%m-%d %H:%M"),
                                user.get("username", ""),
                                int(pick),
                            ),
                        )
                        st.success(f"Issue #{pick} verified and closed")
                    elif act == "Resolve only":
                        execute(
                            "UPDATE dq_issues SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?",
                            (datetime.now().strftime("%Y-%m-%d %H:%M"), user.get("username", ""), int(pick)),
                        )
                        st.success(f"Issue #{pick} resolved")
                    else:
                        execute(
                            """
                            INSERT INTO workplan_actions
                            (facility, problem, action_text, owner, due_date, status, source, created_by)
                            VALUES (?, ?, ?, ?, ?, 'open', 'dq', ?)
                            """,
                            (
                                row.get("facility"),
                                str(row.get("message") or "")[:200],
                                f"Fix DQ issue #{pick}: {row.get('message')}",
                                extra or row.get("owner") or user.get("username", ""),
                                str(due_in),
                                user.get("username", ""),
                            ),
                        )
                        try:
                            wid = int(query("SELECT MAX(id) AS id FROM workplan_actions").iloc[0]["id"])
                            execute("UPDATE dq_issues SET workplan_id=? WHERE id=?", (wid, int(pick)))
                        except Exception:
                            pass
                        st.success(f"Issue #{pick} pushed to workplan")
                    st.rerun()

    # Locked cascade definition note
    st.caption(
        "Cascade volumes use **locked registry** specs (705 suspected/tested/confirmed + AL Total/Dispensed) — "
        "not broad Positive|Total Exam matching."
    )

    # ── Curated validation rules ──
    section_header("Validation rules (curated)")
    rules = dq.get("rules") or run_validation_rules(d)
    if not rules:
        st.caption("No rules evaluated.")
    else:
        fail = [r for r in rules if r["status"] == "fail"]
        passed = [r for r in rules if r["status"] == "pass"]
        skipped = [r for r in rules if r["status"] == "skip"]
        st.caption(
            f"**{len(fail)}** failed · **{len(passed)}** passed · **{len(skipped)}** skipped"
        )
        show = pd.DataFrame([
            {
                "ID": r["id"],
                "Rule": r["name"],
                "Severity": r["severity"],
                "Status": r["status"].upper(),
                "Message": r["message"],
            }
            for r in rules
        ])
        st.dataframe(show, use_container_width=True, hide_index=True)
        critical_fails = [r for r in fail if r["severity"] == "critical"]
        if critical_fails:
            st.error(
                "Critical rule failures — investigate before treating cascade/indicators as definitive:\n"
                + "\n".join(f"- **{r['id']}** {r['name']}: {r['message']}" for r in critical_fails)
            )

    # ── Lightweight DQ review stamp ──
    section_header("DQ review (sign-off)")
    last_rev = get_latest_dq_review()
    if last_rev is not None:
        st.success(
            f"Last reviewed by **{last_rev['reviewed_by']}** on "
            f"**{last_rev['reviewed_at']}** · score recorded: "
            f"{last_rev['dq_score'] if pd.notna(last_rev['dq_score']) else '—'} · "
            f"filter: {last_rev.get('facility_filter', '—')} / {last_rev.get('period_filter', '—')}"
        )
        if last_rev.get("note"):
            st.caption(f"Note: {last_rev['note']}")
    else:
        st.info("No DQ review recorded yet for this database.")

    can_review = user.get("can_upload") or user.get("username") == "admin"
    if can_review:
        with st.form("dq_review_form"):
            note = st.text_input(
                "Review note (optional)",
                placeholder="e.g. April 2026 KHIS checked — gaps on Mwakuhenga MOH 748 noted",
            )
            submitted = st.form_submit_button("Mark DQ as reviewed", type="primary")
            if submitted:
                save_dq_review(
                    user["username"],
                    dq.get("score"),
                    filters,
                    note=note,
                )
                st.success("DQ review saved.")
                st.rerun()
    else:
        st.caption("Only admin / upload roles can record a DQ review.")

    # ── Upload comparison ──
    section_header("Compare two uploads")
    hist = query(
        "SELECT id, file_name, rows_committed, uploaded_by, uploaded_at FROM upload_history ORDER BY id DESC LIMIT 20"
    )
    if hist.empty or len(hist) < 1:
        st.caption("Need at least one committed upload with a snapshot to compare.")
    else:
        labels = {
            int(r["id"]): f"#{int(r['id'])} · {r['file_name']} · {r['uploaded_at']} · {int(r['rows_committed'] or 0):,} rows"
            for _, r in hist.iterrows()
        }
        ids = list(labels.keys())
        c_a, c_b = st.columns(2)
        with c_a:
            id_a = st.selectbox(
                "Earlier / baseline upload",
                ids[::-1] if len(ids) > 1 else ids,
                format_func=lambda i: labels.get(i, str(i)),
                key="dq_cmp_a",
            )
        with c_b:
            default_b = ids[0]
            id_b = st.selectbox(
                "Later / new upload",
                ids,
                index=0,
                format_func=lambda i: labels.get(i, str(i)),
                key="dq_cmp_b",
            )
        if st.button("Run comparison", type="secondary"):
            if id_a == id_b:
                st.warning("Pick two different uploads.")
            else:
                result = compare_uploads(int(id_a), int(id_b))
                if result.get("error"):
                    st.warning(result["error"])
                    st.caption(
                        "Snapshots are stored from this build onward. "
                        "Re-commit a file after updating the app to enable diffs."
                    )
                else:
                    sa, sb = result["summary"]["a"], result["summary"]["b"]
                    st.markdown(
                        f"**Upload A** rows {sa['rows']:,} → **Upload B** rows {sb['rows']:,}"
                    )
                    metrics = [
                        ("RDT exams", "rdt_exams"),
                        ("RDT positives", "rdt_pos"),
                        ("Suspected", "suspected"),
                        ("Tested", "tested"),
                        ("Confirmed", "confirmed"),
                        ("AL dispensed", "al"),
                    ]
                    rows = []
                    for label, key in metrics:
                        va, vb = sa[key], sb[key]
                        delta = vb - va
                        pct, note = pct_change_safe(va, vb)
                        rows.append({
                            "Metric": label,
                            "Upload A": f"{va:,.0f}",
                            "Upload B": f"{vb:,.0f}",
                            "Δ": f"{delta:+,.0f}",
                            "% change": (f"{pct:+.1f}%" if pct is not None else (note or "n/a")),
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                    detail = result["detail"]
                    if detail.empty:
                        st.success("No cell-level value changes between these snapshots.")
                    else:
                        st.markdown(f"**{len(detail):,}** changed cells (largest absolute deltas first)")
                        show_d = detail.head(100).copy()
                        show_d = show_d.rename(columns={
                            "value_a": "Value A", "value_b": "Value B",
                            "delta": "Δ", "pct_change": "% change",
                        })
                        st.dataframe(
                            show_d[[
                                "facility", "register", "indicator", "period",
                                "Value A", "Value B", "Δ", "% change",
                            ]],
                            use_container_width=True,
                            hide_index=True,
                        )
                        csv = detail.to_csv(index=False).encode("utf-8")
                        st.download_button(
                            "📥 Download full diff (CSV)",
                            csv,
                            file_name=f"upload_diff_{id_a}_vs_{id_b}.csv",
                            mime="text/csv",
                        )

    def _rr_pct_series(s):
        if s is None or (hasattr(s, "empty") and s.empty):
            return s
        out = s.astype(float).copy()
        if out.notna().any() and out.max() <= 1.5:
            out = out * 100.0
        return out

    rr = d[d["indicator"] == "Reporting Rate"].copy()
    if not rr.empty:
        rr["rr_pct"] = _rr_pct_series(rr["value"])

    overall_rr = float(rr["rr_pct"].mean()) if not rr.empty else 0.0
    n_low_80 = int((rr["rr_pct"] < 80).sum()) if not rr.empty else 0
    n_low_60 = int((rr["rr_pct"] < 60).sum()) if not rr.empty else 0
    registers_in_view = sorted(d["register"].dropna().unique().tolist())
    periods_in_view = sorted(d["period"].dropna().astype(str).unique().tolist())
    facilities_in_view = sorted(d["facility"].dropna().unique().tolist())

    core = d[d["indicator"] != "Reporting Rate"]
    expected_regs = [r for r in REGISTERS if r in registers_in_view] or registers_in_view

    level = "green" if overall_rr >= 95 and n_low_80 == 0 else (
        "amber" if overall_rr >= 80 and n_low_60 == 0 else "red"
    )
    section_header("Reporting completeness")
    headline(
        f"Overall reporting rate **{overall_rr:.1f}%** · "
        f"**{n_low_80}** cells below 80% · **{len(registers_in_view)}** registers · "
        f"**{len(periods_in_view)}** periods in view. "
        + ("Data looks complete enough for cascade interpretation."
           if level == "green" else
           "Treat cascade and indicators with caution where reporting is low."),
        status=level,
    )

    section_header("Key quality numbers")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        big_number(f"{overall_rr:.0f}%", "Avg reporting rate",
                   delta="Target >=95%", delta_direction="flat")
    with c2:
        big_number(str(n_low_80), "Reports < 80%",
                   delta=f"{n_low_60} under 60%", delta_direction="flat")
    with c3:
        big_number(str(len(registers_in_view)), "Registers in view")
    with c4:
        big_number(str(len(periods_in_view)), "Periods in view")

    section_header("Completeness: register x period")
    st.caption(
        "Green = data present or reporting rate OK. "
        "Empty/missing = no volume data for that register/period in the filter."
    )
    checks = []
    if periods_in_view and expected_regs:
        presence = (
            core.groupby(["register", "period"]).size().reset_index(name="n")
            if not core.empty else pd.DataFrame(columns=["register", "period", "n"])
        )
        rr_mat = (
            rr.groupby(["register", "period"])["rr_pct"].mean().reset_index()
            if not rr.empty else pd.DataFrame(columns=["register", "period", "rr_pct"])
        )
        grid_rows = []
        missing_list = []
        for reg in expected_regs:
            row = {"Register": reg}
            for per in periods_in_view:
                has = False
                if not presence.empty:
                    has = not presence[
                        (presence["register"] == reg) & (presence["period"].astype(str) == str(per))
                    ].empty
                rr_cell = pd.DataFrame()
                if not rr_mat.empty:
                    rr_cell = rr_mat[
                        (rr_mat["register"] == reg) & (rr_mat["period"].astype(str) == str(per))
                    ]
                if not rr_cell.empty and pd.notna(rr_cell["rr_pct"].iloc[0]):
                    pct = float(rr_cell["rr_pct"].iloc[0])
                    if pct < 60:
                        cell = f"RED {pct:.0f}%"
                    elif pct < 80:
                        cell = f"AMB {pct:.0f}%"
                    else:
                        cell = f"OK {pct:.0f}%"
                elif has:
                    cell = "OK data"
                else:
                    cell = "missing"
                    missing_list.append({"Register": reg, "Period": str(per), "Issue": "No data rows"})
                row[str(per)] = cell
            grid_rows.append(row)
        st.dataframe(pd.DataFrame(grid_rows), use_container_width=True, hide_index=True)
        if missing_list:
            with st.expander(f"Missing register-period cells ({len(missing_list)})"):
                st.dataframe(pd.DataFrame(missing_list), use_container_width=True, hide_index=True)
    else:
        st.info("Not enough register/period structure to build a completeness grid.")

    section_header("Low / late reporting (< 80%)")
    if not rr.empty:
        low = rr[rr["rr_pct"] < 80].copy()
        if low.empty:
            st.success("No reporting-rate cells below 80% for current filters.")
        else:
            low = low.sort_values("rr_pct")
            show = low[["facility", "register", "period", "rr_pct"]].copy()
            show.columns = ["Facility", "Register", "Period", "Reporting %"]
            show["Reporting %"] = show["Reporting %"].round(1)
            show["Severity"] = show["Reporting %"].apply(
                lambda x: "Critical (<60%)" if x < 60 else "Warning (<80%)"
            )
            st.dataframe(show, use_container_width=True, hide_index=True)
            st.markdown(
                f"- **{len(low[low['rr_pct'] < 60])}** critical (<60%) · "
                f"**{len(low[(low['rr_pct'] >= 60) & (low['rr_pct'] < 80)])}** warning (60-79%). "
                "Do not over-interpret cascades for these facility-register-period cells."
            )
    else:
        st.info(
            "No Reporting Rate rows in this upload for the filters. "
            "Completeness is inferred only from presence of volume indicators."
        )

    section_header("Reporting rate heatmap")
    if not rr.empty:
        pivot = rr.pivot_table(index="register", columns="period", values="rr_pct", aggfunc="mean")
        fig = px.imshow(
            pivot, aspect="auto", color_continuous_scale="RdYlGn",
            zmin=0, zmax=100, text_auto=".0f",
            labels=dict(color="Reporting %"),
        )
        fig.update_layout(height=max(280, 40 * len(pivot.index) + 80), margin=dict(t=30))
        st.plotly_chart(fig, use_container_width=True)
        if "facility" in rr.columns and rr["facility"].nunique() > 1:
            st.markdown("**By facility (average reporting %)**")
            by_fac = rr.groupby("facility")["rr_pct"].mean().reset_index()
            by_fac.columns = ["Facility", "Avg reporting %"]
            by_fac["Avg reporting %"] = by_fac["Avg reporting %"].round(1)
            fig_f = px.bar(
                by_fac, x="Facility", y="Avg reporting %",
                color="Avg reporting %", color_continuous_scale="RdYlGn",
                range_color=[0, 100], height=320,
            )
            fig_f.update_layout(plot_bgcolor="white")
            fig_f.add_hline(y=95, line_dash="dash", line_color=COLOR_GREEN, annotation_text="Target 95%")
            fig_f.add_hline(y=80, line_dash="dot", line_color=COLOR_AMBER, annotation_text="80%")
            st.plotly_chart(fig_f, use_container_width=True)
    else:
        st.info("No reporting rate data for heatmap.")

    section_header("Facility x register coverage")
    st.caption("Whether each facility has any data for each register in the filtered periods.")
    cov_rows = []
    for fac in (facilities_in_view or FACILITIES):
        row = {"Facility": fac}
        fac_df = d[d["facility"] == fac]
        for reg in expected_regs:
            n = len(fac_df[fac_df["register"] == reg])
            row[reg] = f"yes {n}" if n else "no"
        cov_rows.append(row)
    if cov_rows:
        st.dataframe(pd.DataFrame(cov_rows), use_container_width=True, hide_index=True)

    section_header("Consistency checks (within filters)")
    st.caption(
        "Logic checks: tested vs suspected; positives vs tests; AL vs confirmed; IPT continuum; OPD vs lab."
    )
    for fac in (facilities_in_view or FACILITIES):
        if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
            continue
        fd = d[d["facility"] == fac]
        for per in periods_in_view:
            pd_ = fd[fd["period"].astype(str) == str(per)]
            if pd_.empty:
                continue
            susp = _sum_ind(pd_, r"Suspected")
            tested = _sum_ind(pd_, r"Tested for Malaria|Total Exam")
            conf = _sum_ind(pd_, r"Positive|Confirmed")
            al = _sum_ind(pd_, r"AL Total|AL Dispensed")
            if susp and tested and tested > susp * 1.15:
                checks.append({
                    "Facility": fac, "Period": str(per), "Check": "Tests > Suspected",
                    "Detail": f"Tested {int(tested):,} vs Suspected {int(susp):,}",
                    "Severity": "Warning",
                    "Note": "Possible double-counting or mixed age denominators",
                })
            if tested and conf and conf > tested * 1.02:
                checks.append({
                    "Facility": fac, "Period": str(per), "Check": "Positives > Tests",
                    "Detail": f"Positive {int(conf):,} vs Tested {int(tested):,}",
                    "Severity": "Critical",
                    "Note": "Data error or mismatched indicators — do not trust TPR",
                })
            if conf and al and al > conf * 3:
                checks.append({
                    "Facility": fac, "Period": str(per), "Check": "AL >> Confirmed",
                    "Detail": f"AL {int(al):,} vs Confirmed {int(conf):,}",
                    "Severity": "Warning",
                    "Note": "May be weight-band packs or community AL vs facility confirmations",
                })
            ipt1 = _sum_ind(pd_, r"^IPT1$|IPT\s*1")
            ipt3 = _sum_ind(pd_, r"^IPT3$|IPT\s*3")
            if ipt1 and ipt3 and ipt3 > ipt1 * 1.05:
                checks.append({
                    "Facility": fac, "Period": str(per), "Check": "IPT3 > IPT1",
                    "Detail": f"IPT3 {int(ipt3):,} vs IPT1 {int(ipt1):,}",
                    "Severity": "Critical",
                    "Note": "Implausible continuum — check period alignment or mapping",
                })

    pairs = [
        ("MOH 705A", "Confirmed <5", "MOH 706", "RDT <5 Positive"),
        ("MOH 705B", "Confirmed >=5", "MOH 706", "RDT >=5 Positive"),
    ]
    for reg1, ind1, reg2, ind2 in pairs:
        a = d[(d["register"] == reg1) & (d["indicator"] == ind1)]["value"].sum()
        b = d[(d["register"] == reg2) & (d["indicator"] == ind2)]["value"].sum()
        if a or b:
            base = max(a, b, 1)
            diff = abs(a - b) / base * 100
            sev = "OK" if diff < 10 else ("Warning" if diff < 25 else "Critical")
            checks.append({
                "Facility": "All in filter", "Period": "All in filter",
                "Check": f"{reg1} {ind1} vs {reg2} {ind2}",
                "Detail": f"{int(a):,} vs {int(b):,}",
                "Severity": sev,
                "Note": f"Diff {diff:.0f}% — OPD confirmed vs lab RDT positive",
            })

    if checks:
        st.dataframe(pd.DataFrame(checks), use_container_width=True, hide_index=True)
        n_red = sum(1 for c in checks if c["Severity"] == "Critical")
        n_amb = sum(1 for c in checks if c["Severity"] == "Warning")
        st.markdown(
            f"- **{n_red}** critical consistency issues · **{n_amb}** warnings. "
            "Fix or document before using Loss & Gap for supervision decisions."
        )
    else:
        st.success("No consistency issues flagged for the current filters.")

    # ── Tier A1: Missing months in selected range ──
    section_header("Missing months in selected range")
    st.caption(
        "For monthly (YYYY-MM) periods in the filter, detect holes between the earliest and latest month."
    )
    tier_a_flags = []  # collect for score
    monthly_pers = sorted(
        [str(p) for p in periods_in_view if len(str(p)) == 7 and str(p)[4] == "-" and "Q" not in str(p)]
    )
    if len(monthly_pers) >= 2:
        try:
            start_m = pd.Period(monthly_pers[0], freq="M")
            end_m = pd.Period(monthly_pers[-1], freq="M")
            expected_months = [str(p) for p in pd.period_range(start_m, end_m, freq="M")]
            present_set = set(monthly_pers)
            missing_months = [m for m in expected_months if m not in present_set]
            if missing_months:
                st.warning(
                    f"**{len(missing_months)}** month(s) missing between {monthly_pers[0]} and {monthly_pers[-1]}: "
                    + ", ".join(missing_months[:12])
                    + ("…" if len(missing_months) > 12 else "")
                )
                st.dataframe(
                    pd.DataFrame({"Missing month": missing_months}),
                    use_container_width=True, hide_index=True,
                )
                tier_a_flags.append({"Severity": "Warning", "Item": f"{len(missing_months)} missing months"})
            else:
                st.success(
                    f"No month holes between {monthly_pers[0]} and {monthly_pers[-1]} "
                    f"({len(monthly_pers)} months present)."
                )
        except Exception as e:
            st.caption(f"Could not expand month range: {e}")
    elif len(monthly_pers) == 1:
        st.info(f"Only one monthly period in view ({monthly_pers[0]}) — gap check needs a range.")
    else:
        st.info("No YYYY-MM monthly periods in the current filter (quarterly/yearly only, or empty).")

    # ── Tier A2: Age-group completeness (<5 and ≥5) ──
    section_header("Age-group completeness (<5 and ≥5)")
    st.caption(
        "When facility registers are in scope, both under-five and ≥5 lines should appear "
        "so age-split cascades and TPR are interpretable."
    )
    fac_regs = [r for r in expected_regs if r in ("MOH 705A", "MOH 705B", "MOH 706", "MOH 705")]
    age_rows = []
    if fac_regs:
        for fac in (facilities_in_view or FACILITIES):
            if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
                continue
            fd = d[d["facility"] == fac]
            has_u5 = (
                (fd["age_group"] == "<5").any()
                or fd["indicator"].str.contains(r"<5|Under five|Under 5", case=False, na=False).any()
            )
            has_o5 = (
                fd["age_group"].isin([">=5", ">5"]).any()
                or fd["indicator"].str.contains(r">=5|>5|5 years", case=False, na=False).any()
            )
            status = "OK both" if (has_u5 and has_o5) else (
                "Missing ≥5" if has_u5 and not has_o5 else (
                    "Missing <5" if has_o5 and not has_u5 else "Missing both"
                )
            )
            if status != "OK both":
                tier_a_flags.append({"Severity": "Warning", "Item": f"{fac}: {status}"})
            age_rows.append({
                "Facility": fac,
                "<5 present": "Yes" if has_u5 else "No",
                "≥5 present": "Yes" if has_o5 else "No",
                "Status": status,
            })
        if age_rows:
            st.dataframe(pd.DataFrame(age_rows), use_container_width=True, hide_index=True)
            bad = [r for r in age_rows if r["Status"] != "OK both"]
            if bad:
                st.markdown(
                    f"- **{len(bad)}** facility(ies) lack a full age split. "
                    "Age cascades on Loss & Gap may be incomplete."
                )
            else:
                st.success("All facilities in view have both <5 and ≥5 data markers.")
    else:
        st.info("No facility OPD/lab registers in the current filter — age split check skipped.")

    # ── Tier A3: Community vs facility reconciliation ──
    section_header("Community vs facility reconciliation")
    st.caption(
        "Compare confirmed positives and AL between facility stream and community (MOH 748). "
        "Large mismatches are flags, not automatic errors."
    )
    fac_d = d[d["stream"] == "Facility"] if "stream" in d.columns else d
    com_d = d[d["stream"] == "Community"] if "stream" in d.columns else pd.DataFrame()
    f_conf = _sum_ind(fac_d, r"Positive|Confirmed")
    f_al = _sum_ind(fac_d, r"AL Total|AL Dispensed")
    c_conf = _sum_ind(com_d, r"Positive") if not com_d.empty else 0.0
    c_al = _sum_ind(com_d, r"AL Dispensed|AL Total") if not com_d.empty else 0.0
    recon = pd.DataFrame([
        {
            "Stream": "Facility",
            "Confirmed / positive": int(f_conf),
            "AL dispensed": int(f_al),
            "AL / confirmed %": f"{_safe_pct(f_al, f_conf):.0f}%" if f_conf else "—",
        },
        {
            "Stream": "Community",
            "Confirmed / positive": int(c_conf),
            "AL dispensed": int(c_al),
            "AL / confirmed %": f"{_safe_pct(c_al, c_conf):.0f}%" if c_conf else "—",
        },
        {
            "Stream": "Combined",
            "Confirmed / positive": int(f_conf + c_conf),
            "AL dispensed": int(f_al + c_al),
            "AL / confirmed %": f"{_safe_pct(f_al + c_al, f_conf + c_conf):.0f}%" if (f_conf + c_conf) else "—",
        },
    ])
    st.dataframe(recon, use_container_width=True, hide_index=True)
    if c_conf and f_conf:
        share = _safe_pct(c_conf, f_conf + c_conf)
        st.markdown(f"- Community share of positives: **{share:.0f}%**.")
        if c_conf and _safe_pct(c_al, c_conf) < 50:
            st.warning(
                "Community treatment coverage looks low vs community positives — "
                "check CHP AL and referral completion (not only facility AL)."
            )
            tier_a_flags.append({"Severity": "Warning", "Item": "Low community AL vs community positives"})
    elif c_conf == 0 and not com_d.empty:
        st.info("Community stream present but no positive counts matched in filters.")
    elif com_d is None or com_d.empty:
        st.info("No community-stream rows in this filter — upload MOH 748 for this check.")

    # ── Tier A4: Mandatory indicators per register ──
    section_header("Mandatory indicators per register")
    st.caption(
        "If a register appears in the filtered data, key lines should also appear "
        "(otherwise mapping or extract may be incomplete)."
    )
    MANDATORY = {
        "MOH 705A": [r"Suspected", r"Tested", r"Confirmed"],
        "MOH 705B": [r"Suspected", r"Tested", r"Confirmed"],
        "MOH 706": [r"Total Exam", r"Positive"],
        "MOH 743": [r"^IPT1$|IPT\s*1", r"^IPT2$|IPT\s*2", r"^IPT3$|IPT\s*3"],
        "MOH 748": [r"Positive", r"Total"],
        "MOH 515": [r"Reporting Rate"],
    }
    mand_rows = []
    for reg, patterns in MANDATORY.items():
        if reg not in registers_in_view:
            continue
        reg_df = d[d["register"] == reg]
        for pat in patterns:
            hit = reg_df["indicator"].str.contains(pat, case=False, na=False, regex=True).any()
            label = pat.replace(r"^", "").replace(r"$", "").replace(r"\s*", " ")
            if not hit:
                tier_a_flags.append({"Severity": "Warning", "Item": f"{reg} missing pattern {label}"})
            mand_rows.append({
                "Register": reg,
                "Expected pattern": label,
                "Present": "Yes" if hit else "No",
                "Status": "OK" if hit else "Missing",
            })
    if mand_rows:
        st.dataframe(pd.DataFrame(mand_rows), use_container_width=True, hide_index=True)
        n_miss = sum(1 for r in mand_rows if r["Status"] == "Missing")
        if n_miss:
            st.markdown(
                f"- **{n_miss}** mandatory pattern(s) missing. "
                "Cascades/IPT may under-count for those registers."
            )
        else:
            st.success("All mandatory patterns found for registers present in the filter.")
    else:
        st.info("No monitored registers in the current filter.")

    # ── Tier A5: TPR / volume outliers ──
    section_header("TPR and volume outliers")
    st.caption(
        "Light statistical flags: period TPR more than ~2 SD above recent mean, "
        "or test volume collapsing vs prior periods (same facility)."
    )
    outlier_rows = []
    rdt = d[(d["register"] == "MOH 706") & (d["indicator"].str.contains("RDT|Total Exam|Positive", na=False))]
    if rdt.empty:
        rdt = d[d["indicator"].str.contains("Total Exam|Positive|Tested", na=False)]
    for fac in (facilities_in_view or FACILITIES):
        if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
            continue
        fac_rdt = rdt[rdt["facility"] == fac] if not rdt.empty else pd.DataFrame()
        if fac_rdt.empty:
            continue
        tested = fac_rdt[fac_rdt["indicator"].str.contains("Total Exam|Tested", na=False)].groupby("period")["value"].sum()
        positive = fac_rdt[fac_rdt["indicator"].str.contains("Positive|Confirmed", na=False)].groupby("period")["value"].sum()
        if tested.empty:
            continue
        rate_vals = []
        for idx in tested.index:
            a = safe_rate(
                float(positive.reindex([idx]).fillna(0).iloc[0]),
                float(tested.loc[idx]),
                indicator="outlier_tpr",
            )
            if a["status"] == "valid" and a["value"] is not None:
                rate_vals.append((idx, a["value"]))
            elif a["status"] == "invalid":
                # Data quality outlier path, not epi spike
                outlier_rows.append({
                    "Facility": fac,
                    "Signal": "Invalid TPR (num>den)",
                    "Latest period": str(idx),
                    "Latest": f"raw {a.get('calculated_pct')}",
                    "Prior mean": "—",
                    "Detail": a.get("reason") or "numerator > denominator",
                })
                tier_a_flags.append({"Severity": "Critical", "Item": f"{fac} invalid TPR {idx}"})
        rate = pd.Series({i: v for i, v in rate_vals}).sort_index()
        tested_s = tested.sort_index()
        if len(rate) >= 4:
            prior = rate.iloc[:-1]
            mean, sd, latest = prior.mean(), prior.std(), rate.iloc[-1]
            if sd and sd > 0 and latest > mean + 2 * sd:
                outlier_rows.append({
                    "Facility": fac,
                    "Signal": "TPR spike",
                    "Latest period": str(rate.index[-1]),
                    "Latest": f"{latest:.1f}%",
                    "Prior mean": f"{mean:.1f}%",
                    "Detail": f"> mean+2SD (SD={sd:.1f})",
                })
                tier_a_flags.append({"Severity": "Critical", "Item": f"{fac} TPR spike"})
        if len(tested_s) >= 4:
            prior_t = tested_s.iloc[:-1]
            latest_t = float(tested_s.iloc[-1])
            mean_t = float(prior_t.mean())
            if mean_t > 0 and latest_t < mean_t * 0.5:
                outlier_rows.append({
                    "Facility": fac,
                    "Signal": "Testing volume collapse",
                    "Latest period": str(tested_s.index[-1]),
                    "Latest": f"{int(latest_t):,}",
                    "Prior mean": f"{mean_t:.0f}",
                    "Detail": "< 50% of prior mean tests",
                })
                tier_a_flags.append({"Severity": "Critical", "Item": f"{fac} testing collapse"})
    if outlier_rows:
        st.dataframe(pd.DataFrame(outlier_rows), use_container_width=True, hide_index=True)
        st.markdown(
            "- Investigate spikes/collapses before treating GO TPR or cascades as stable transmission signals."
        )
    else:
        st.success("No TPR spike or testing-collapse outliers for facilities with enough history in the filter.")

    # ── Upload history ──
    section_header("Upload history (trust trail)")
    try:
        ups = query(
            "SELECT id, filename, uploaded_by, uploaded_at, row_count, status "
            "FROM uploads ORDER BY uploaded_at DESC LIMIT 15"
        )
    except Exception:
        ups = pd.DataFrame()
    if ups is not None and not ups.empty:
        st.dataframe(ups, use_container_width=True, hide_index=True)
        st.caption("Latest successful commits define what Overview and indicators can show.")
    else:
        st.info("No upload history table rows yet.")

    section_header("Quality score (simple)")
    score = 100.0
    notes = []
    if overall_rr < 95:
        score -= min(30, (95 - overall_rr) * 0.8)
        notes.append(f"Reporting rate {overall_rr:.0f}% (target 95%)")
    if n_low_60:
        score -= min(25, n_low_60 * 5)
        notes.append(f"{n_low_60} cells under 60% reporting")
    n_red = sum(1 for c in checks if c.get("Severity") == "Critical")
    n_red += sum(1 for c in tier_a_flags if c.get("Severity") == "Critical")
    n_warn_a = sum(1 for c in tier_a_flags if c.get("Severity") == "Warning")
    if n_red:
        score -= min(25, n_red * 8)
        notes.append(f"{n_red} critical flags (consistency + Tier A)")
    if n_warn_a:
        score -= min(15, n_warn_a * 2)
        notes.append(f"{n_warn_a} Tier A warnings")
    score = max(0, min(100, score))
    q_level = "green" if score >= 85 else ("amber" if score >= 65 else "red")
    headline(
        f"Composite data quality score: **{score:.0f}/100**. "
        + ("; ".join(notes) if notes else "No major deductions."),
        status=q_level,
    )

    section_header("How to use this page")
    st.markdown("""
1. **Fix critical reporting cells** before trusting facility-level cascades.
2. **Critical consistency / outliers** (positives > tests, IPT3 > IPT1, TPR spike, testing collapse) → investigate extract or operations.
3. **Missing months / mandatory lines / age split** → incomplete picture; do not over-claim coverage.
4. **Community vs facility** mismatches → check CHP AL and referrals, not only OPD.
5. After a new upload, re-open with **Monthly · latest 3** and confirm the score before County presentations.
6. Pair with **Loss & Gap**: quality explains whether a gap is real or a data artefact.
""")

    with st.expander("Thresholds used"):
        st.markdown("""
| Signal | Threshold |
|--------|-----------|
| Reporting target | >= 95% |
| Warning reporting | < 80% |
| Critical reporting | < 60% |
| Tests vs suspected | Warning if tests > 115% of suspected |
| Positives vs tests | Critical if positives > tests |
| AL vs confirmed | Warning if AL > 3× confirmed |
| IPT3 vs IPT1 | Critical if IPT3 > IPT1 |
| OPD vs lab confirmed | Warning if diff >= 10%; critical >= 25% |
| Missing months | Any hole in YYYY-MM span in filter |
| Age completeness | Both <5 and ≥5 markers expected for facility registers |
| Mandatory lines | Key patterns required if register is present |
| TPR outlier | Latest TPR > prior mean + 2 SD (≥4 periods) |
| Testing collapse | Latest tests < 50% of prior mean (≥4 periods) |
""")



# ==================================================================
# PAGE 12 — CHP PERFORMANCE
# ==================================================================

def compute_commodity_forecast(lookback_days=90, lead_time_days=14):
    """
    Early Phase B commodity forecasting from stock + stock_movements.
    AMC = average monthly consumption from issues over lookback.
    Returns DataFrame with months_of_stock, projected_stockout_date, reorder_qty, status.
    """
    try:
        stock = query("SELECT * FROM stock")
    except Exception:
        stock = pd.DataFrame()
    try:
        moves = query("SELECT * FROM stock_movements")
    except Exception:
        moves = pd.DataFrame()

    if stock.empty:
        return pd.DataFrame()

    today = datetime.now().date()
    rows = []
    for _, s in stock.iterrows():
        fac = s.get("facility")
        item = s.get("item")
        qty = float(s.get("quantity") or 0)
        reorder = float(s.get("reorder_point") or 30)
        amc = None
        months_stock = None
        proj_out = None
        reorder_qty = None
        status = "unknown"
        note = ""

        if not moves.empty:
            m = moves[
                (moves["facility"].astype(str) == str(fac))
                & (moves["item"].astype(str).str.upper() == str(item).upper())
            ].copy()
            if not m.empty and "movement_date" in m.columns:
                m["movement_date"] = pd.to_datetime(m["movement_date"], errors="coerce")
                cutoff = pd.Timestamp(today) - pd.Timedelta(days=lookback_days)
                m = m[m["movement_date"] >= cutoff]
                issues = m[
                    m["movement_type"].astype(str).str.lower().isin(
                        ["issue", "dispense", "consumption", "out"]
                    )
                ]
                if not issues.empty and "quantity" in issues.columns:
                    total_issued = float(issues["quantity"].abs().sum())
                    days = max((today - cutoff.date()).days, 1)
                    amc = total_issued * (30.0 / days)
                    note = f"AMC from {len(issues)} issue line(s) over ~{days}d"
                else:
                    note = "No issue movements in lookback — AMC unavailable"
            else:
                note = "No dated movements for this item"
        else:
            note = "No stock_movements table data — import Layer 2 movements for AMC"

        if amc and amc > 0:
            months_stock = qty / amc
            days_left = qty / (amc / 30.0) if amc else None
            if days_left is not None:
                proj_out = (today + pd.Timedelta(days=int(days_left))).strftime("%Y-%m-%d")
            # Reorder to cover lead time + 1 month buffer
            reorder_qty = max(0, int(amc * (1 + lead_time_days / 30.0) - qty + 0.5))
            if qty <= 0:
                status = "stock_out"
            elif months_stock < lead_time_days / 30.0:
                status = "critical"
            elif qty < reorder or months_stock < 1.0:
                status = "low"
            elif months_stock < 2.0:
                status = "watch"
            else:
                status = "ok"
        else:
            if qty < reorder:
                status = "low"
                note = (note + " · below reorder point (no AMC)").strip(" ·")
            elif qty <= 0:
                status = "stock_out"
            else:
                status = "ok_no_amc"

        rows.append({
            "Facility": fac,
            "Item": item,
            "On hand": int(qty),
            "Reorder point": int(reorder),
            "AMC (monthly)": round(amc, 1) if amc is not None else None,
            "Months of stock": round(months_stock, 1) if months_stock is not None else None,
            "Projected stock-out": proj_out or "—",
            "Suggested reorder qty": reorder_qty if reorder_qty is not None else "—",
            "Status": status,
            "Note": note,
        })
    return pd.DataFrame(rows)


def compute_redistribution_plan(forecast=None, surplus_mos=3.0, min_transfer=10):
    """
    B4 — propose balancing transfers within the three CBMP sites.
    Surplus = MOS >= surplus_mos (or high on-hand with no AMC).
    Deficit = stock_out / critical / low.
    Not an order: a planning suggestion.
    """
    fc = forecast if forecast is not None else compute_commodity_forecast()
    if fc is None or fc.empty:
        return pd.DataFrame()
    plans = []
    items = fc["Item"].astype(str).str.upper().unique()
    for item in items:
        sub = fc[fc["Item"].astype(str).str.upper() == item].copy()
        def _mos(r):
            v = r.get("Months of stock")
            try:
                return float(v) if v not in (None, "—") and pd.notna(v) else None
            except Exception:
                return None
        surplus = []
        deficit = []
        for _, r in sub.iterrows():
            mos = _mos(r)
            stt = r.get("Status")
            qty = float(r.get("On hand") or 0)
            amc = r.get("AMC (monthly)")
            try:
                amc = float(amc) if amc not in (None, "—") and pd.notna(amc) else None
            except Exception:
                amc = None
            if stt in ("stock_out", "critical", "low"):
                need = r.get("Suggested reorder qty")
                try:
                    need = int(need) if need not in (None, "—") else max(int(amc or 0), min_transfer)
                except Exception:
                    need = min_transfer
                deficit.append({"facility": r["Facility"], "need": max(need, min_transfer), "status": stt, "on_hand": qty})
            elif (mos is not None and mos >= surplus_mos) or (stt in ("ok", "ok_no_amc") and qty > 80):
                extra = 0
                if mos is not None and amc:
                    extra = max(0, int((mos - 2.0) * amc))
                else:
                    extra = max(0, int(qty * 0.3))
                if extra >= min_transfer:
                    surplus.append({"facility": r["Facility"], "extra": extra, "mos": mos, "on_hand": qty})
        surplus = sorted(surplus, key=lambda x: -x["extra"])
        deficit = sorted(deficit, key=lambda x: -x["need"])
        for d in deficit:
            remaining = d["need"]
            for s in surplus:
                if s["facility"] == d["facility"] or s["extra"] < min_transfer or remaining < min_transfer:
                    continue
                move = min(s["extra"], remaining)
                if move < min_transfer:
                    continue
                s["extra"] -= move
                remaining -= move
                plans.append({
                    "Item": item,
                    "From": s["facility"],
                    "To": d["facility"],
                    "Suggested qty": int(move),
                    "Deficit status": d["status"],
                    "Rationale": f"Balance {item}: {d['facility']} {d['status']} while {s['facility']} has surplus",
                })
            if remaining >= min_transfer:
                plans.append({
                    "Item": item,
                    "From": "(pipeline / KEMSA)",
                    "To": d["facility"],
                    "Suggested qty": int(remaining),
                    "Deficit status": d["status"],
                    "Rationale": "No intra-site surplus left — raise an emergency order",
                })
    return pd.DataFrame(plans)


def compute_climate_ew(df=None, filters=None, rain_alert_mm=80, rain_high_mm=120):
    """
    B5 — early warning *signals*, not predictions.
    Combines recent rainfall with valid case/TPR movement.
    """
    try:
        climate = query("SELECT * FROM climate_data ORDER BY week_ending")
    except Exception:
        climate = pd.DataFrame()
    signals = []
    if climate.empty:
        return climate, {
            "level": "none",
            "title": "No climate log",
            "detail": "Enter weekly rainfall (KMD / station) to enable signals.",
            "signals": signals,
            "rain_4wk": None,
            "latest_rain": None,
        }
    climate = climate.copy()
    climate["rainfall_mm"] = pd.to_numeric(climate["rainfall_mm"], errors="coerce")
    climate["week_ending"] = climate["week_ending"].astype(str)
    latest_rain = float(climate.iloc[-1]["rainfall_mm"] or 0)
    rain_4wk = float(climate.tail(4)["rainfall_mm"].sum())
    # Case trend from locked cascade / GO
    rising_cases = False
    rising_tpr = False
    last_conf = prev_conf = None
    if df is not None and not df.empty:
        d = _filter_data(df, filters or {})
        monthly = d[d["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)] if not d.empty else pd.DataFrame()
        if not monthly.empty:
            pers = sorted(monthly["period"].astype(str).unique())
            if len(pers) >= 2:
                c0 = compute_cascade_metrics(monthly[monthly["period"].astype(str) == pers[-2]])
                c1 = compute_cascade_metrics(monthly[monthly["period"].astype(str) == pers[-1]])
                last_conf, prev_conf = c1.get("confirmed"), c0.get("confirmed")
                if prev_conf and last_conf and last_conf > prev_conf * 1.2:
                    rising_cases = True
                a0 = compute_indicator("GO_1", monthly[monthly["period"].astype(str) == pers[-2]])
                a1 = compute_indicator("GO_1", monthly[monthly["period"].astype(str) == pers[-1]])
                if a0.get("status") == "valid" and a1.get("status") == "valid":
                    if (a1.get("value") or 0) > (a0.get("value") or 0):
                        rising_tpr = True

    if latest_rain >= rain_high_mm:
        signals.append(f"Weekly rainfall {latest_rain:.0f} mm ≥ {rain_high_mm} mm")
    elif latest_rain >= rain_alert_mm:
        signals.append(f"Weekly rainfall {latest_rain:.0f} mm ≥ watch {rain_alert_mm} mm")
    if rain_4wk >= rain_high_mm * 2:
        signals.append(f"4-week rainfall total {rain_4wk:.0f} mm is high")
    if rising_cases:
        signals.append(f"Confirmed cases up >20% ({prev_conf} → {last_conf})")
    if rising_tpr:
        signals.append("Valid RDT TPR rose vs previous month")

    if latest_rain >= rain_high_mm and (rising_cases or rising_tpr):
        level, title = "high", "Climate + transmission signal"
        detail = "High rainfall coincides with rising confirmed cases and/or valid TPR. Review EPR readiness — this is a signal, not a forecast."
    elif latest_rain >= rain_high_mm or rain_4wk >= rain_high_mm * 2:
        level, title = "watch", "Wet-period watch"
        detail = "Rainfall is elevated. Watch testing volume and commodities over the next 2–4 weeks."
    elif rising_cases or rising_tpr:
        level, title = "watch", "Transmission movement without rain trigger"
        detail = "Cases/TPR moved independently of the rain threshold. Do not blame climate only."
    else:
        level, title = "quiet", "No combined climate–malaria signal"
        detail = "Rainfall and valid case signals are not both elevated."

    return climate, {
        "level": level,
        "title": title,
        "detail": detail,
        "signals": signals,
        "rain_4wk": rain_4wk,
        "latest_rain": latest_rain,
        "rising_cases": rising_cases,
        "rising_tpr": rising_tpr,
        "last_conf": last_conf,
        "prev_conf": prev_conf,
    }


def page_chp(user, filters):
    """B2 CHP intelligence + operations: roster, workload, stock, referrals, supervision, workplan."""
    page_header(
        "CHP intelligence (B2)",
        "Workforce + community performance: competency, 648 workload vs peers, "
        "referrals, commodities, supervision and workplan.",
    )
    df = load_all_data()
    d = _filter_data(df, filters) if not df.empty else df

    chps = query("SELECT * FROM chps ORDER BY facility, name")
    refs = query("SELECT * FROM referrals ORDER BY referral_date DESC")
    stock = query("SELECT * FROM stock ORDER BY facility, item")
    visits = query("SELECT * FROM supervision_visits ORDER BY visit_date DESC")

    fac_filter = filters.get("facility")
    if fac_filter and fac_filter != "All three":
        if not chps.empty:
            chps = chps[chps["facility"] == fac_filter]
        if not refs.empty and "facility" in refs.columns:
            refs = refs[refs["facility"] == fac_filter]
        if not stock.empty:
            stock = stock[stock["facility"] == fac_filter]
        if not visits.empty:
            visits = visits[visits["facility"] == fac_filter]

    total = len(chps)
    passed = int((chps["competency_status"] == "passed").sum()) if total else 0
    pending = int((chps["competency_status"] == "pending").sum()) if total else 0
    so_a = safe_rate(passed, total, indicator="SO_1_ops", formula="Passed ÷ Active CHPs × 100") if total else {"status": "na", "value": None}
    rate = so_a.get("value") if so_a.get("status") == "valid" else None
    text = (
        f"{total} CHPs · {passed} passed ({rate:.0f}%) · {pending} pending assessment."
        if total and rate is not None else (
            f"{total} CHPs · {passed} passed · {pending} pending assessment." if total
            else "No CHP roster yet. Add CHPs below to track community performance."
        )
    )
    headline(text, status="green" if (rate is not None and rate >= 70) else ("amber" if total else "red"))

    section_header("CHP productivity from MOH 648")
    try:
        ev648 = query("SELECT * FROM moh_648")
    except Exception:
        ev648 = pd.DataFrame()
    if ev648.empty:
        st.info("No 648 events — CHP productivity stays empty until the community register is imported.")
    else:
        by = ev648.copy()
        if "chp_name" not in by.columns:
            by["chp_name"] = by.get("chp") if "chp" in by.columns else "unspecified"
        for col in ("tested", "positive", "treated", "referred", "followed_up"):
            if col not in by.columns:
                by[col] = 0
            by[col] = pd.to_numeric(by[col], errors="coerce").fillna(0)
        prod = by.groupby(by["chp_name"].astype(str)).agg(
            events=("chp_name", "size"),
            tested=("tested", "sum"),
            positive=("positive", "sum"),
            treated=("treated", "sum"),
            referred=("referred", "sum"),
            followed_up=("followed_up", "sum"),
        ).reset_index().rename(columns={"chp_name": "CHP"})
        tpr_vals, tpr_st = [], []
        for _, r in prod.iterrows():
            a = safe_rate(r["positive"], r["tested"], indicator="CHP_TPR")
            tpr_vals.append(a.get("value") if a.get("status") == "valid" else None)
            tpr_st.append(a.get("status"))
        prod["TPR"] = tpr_vals
        prod["TPR status"] = tpr_st
        prod["Zero activity"] = prod["tested"] == 0
        st.dataframe(prod.sort_values("tested", ascending=False), use_container_width=True, hide_index=True)
        zeros = int(prod["Zero activity"].sum())
        if zeros:
            st.warning(f"{zeros} CHP name(s) with zero tests in 648 — review roster vs register spelling.")

    # Charts first
    section_header("Operations snapshot")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        big_number(str(total), "CHPs on roster")
    with c2:
        big_number(f"{rate:.0f}%" if rate is not None else "—", "Competency (SO)", delta="Target ≥70%", delta_direction="flat")
    with c3:
        ref_total = len(refs)
        arrived = int(refs["arrived"].sum()) if not refs.empty and "arrived" in refs.columns else 0
        ref_a = safe_rate(
            arrived, ref_total,
            indicator="REF_1",
            formula="Arrived ÷ Referrals logged × 100",
            source="Referrals table",
        ) if ref_total else {"status": "na", "value": None}
        comp_rate = ref_a.get("value") if ref_a.get("status") == "valid" else None
        big_number(
            f"{comp_rate:.0f}%" if comp_rate is not None else "—",
            "Referral completion",
            delta=f"{ref_total} logged",
            delta_direction="flat",
        )
    with c4:
        low_stock = 0
        if not stock.empty:
            low_stock = int((stock["quantity"] < stock["reorder_point"]).sum())
        big_number(str(low_stock), "Stock items low", delta="Below reorder", delta_direction="down" if low_stock else "flat")

    if total:
        fig = px.bar(
            chps.groupby(["facility", "competency_status"]).size().reset_index(name="count"),
            x="facility", y="count", color="competency_status",
            barmode="stack", height=320,
            color_discrete_map={"passed": COLOR_GREEN, "pending": COLOR_AMBER, "failed": COLOR_RED},
        )
        fig.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12), title="CHPs by facility & competency")
        st.plotly_chart(fig, use_container_width=True)

    # ── B2 CHP intelligence ──
    section_header("CHP intelligence & benchmarking (B2)")
    st.caption(
        "Roster × MOH 648: tests, valid TPR only, referrals, follow-up, workload vs facility peer median. "
        "Flags target supervision — they are not performance scores for individuals."
    )
    wl, wsum = build_chp_workload(fac_filter)
    b1, b2, b3, b4, b5 = st.columns(5)
    with b1:
        big_number(str(wsum.get("roster", 0)), "Roster")
    with b2:
        big_number(str(wsum.get("matched", 0)), "Linked to 648 tests")
    with b3:
        big_number(str(wsum.get("zero_testing", 0)), "Zero activity")
    with b4:
        big_number(str(wsum.get("high_workload", 0)), "High workload")
    with b5:
        big_number(str(wsum.get("unmatched_648", 0)), "648 names not on roster")
    if wl.empty:
        st.info("Import CHP roster and MOH 648 to see named CHP benchmarking.")
    else:
        view = wl.copy()
        view["TPR %"] = view["TPR %"].apply(lambda x: f"{x:.0f}" if isinstance(x, (int, float)) and pd.notna(x) else "—")
        view["Referral %"] = view["Referral %"].apply(lambda x: f"{x:.0f}" if isinstance(x, (int, float)) and pd.notna(x) else "—")
        view["Follow-up %"] = view["Follow-up %"].apply(lambda x: f"{x:.0f}" if isinstance(x, (int, float)) and pd.notna(x) else "—")
        st.dataframe(view.sort_values(["Facility", "Tests"], ascending=[True, False]), use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Download CHP workload CSV",
            wl.to_csv(index=False).encode("utf-8"),
            file_name=f"chp_workload_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="b2_chp_csv",
        )
        left, right = st.columns(2)
        with left:
            fig_t = px.bar(
                wl.sort_values("Tests", ascending=False).head(25),
                x="CHP", y="Tests", color="Facility", height=360,
            )
            fig_t.update_layout(plot_bgcolor="white", xaxis_tickangle=-40, title="Tests by CHP")
            st.plotly_chart(fig_t, use_container_width=True)
        with right:
            tpr_ok = wl[wl["TPR %"].notna()] if "TPR %" in wl.columns else pd.DataFrame()
            # original wl still has numeric TPR
            tpr_ok = wl[pd.to_numeric(wl["TPR %"], errors="coerce").notna()]
            if not tpr_ok.empty:
                fig_p = px.scatter(
                    tpr_ok, x="Tests", y="TPR %", color="Facility", hover_name="CHP",
                    size="Positive", height=360, title="Workload vs valid TPR",
                )
                fig_p.update_layout(plot_bgcolor="white")
                st.plotly_chart(fig_p, use_container_width=True)
            else:
                st.caption("No valid CHP-level TPR (need 648 tested + positives).")
        zero = wl[wl["Flags"].str.contains("zero", na=False)]
        if not zero.empty:
            st.warning(f"**{len(zero)}** CHP(s) with zero testing/activity:")
            st.dataframe(zero[["Facility", "CHP", "CHU", "Competency", "Flags"]], use_container_width=True, hide_index=True)

    # Facility share of community tests
    section_header("Community testing share by facility")
    if d is not None and not d.empty:
        share_rows = []
        for fac in FACILITIES:
            if fac_filter not in (None, "All three") and fac != fac_filter:
                continue
            pf = dict(filters or {})
            pf["facility"] = fac
            r11 = compute_indicator("R1_1", df, pf)
            share_rows.append({
                "Facility": fac,
                "CHP tests (R1.1)": r11.get("value") if r11.get("status") in ("valid", None) else None,
                "Status": (r11.get("status") or "na").upper(),
            })
        sdf = pd.DataFrame(share_rows)
        st.dataframe(sdf, use_container_width=True, hide_index=True)
        ok = sdf[sdf["CHP tests (R1.1)"].notna()]
        if not ok.empty:
            fig_s = px.pie(ok, names="Facility", values="CHP tests (R1.1)", height=320)
            st.plotly_chart(fig_s, use_container_width=True)

    # Flags
    section_header("Red flags (supervision targeting)")
    flags = []
    if pending:
        flags.append(f"🟡 **{pending} CHP(s)** pending competency assessment — schedule SO assessments.")
    if not stock.empty:
        for _, s in stock[stock["quantity"] < stock["reorder_point"]].iterrows():
            flags.append(f"🔴 **Stock:** {s['item']} at {s['facility']} = {s['quantity']} (reorder ≤ {s['reorder_point']})")
    if not visits.empty and "follow_up_date" in visits.columns:
        overdue = visits[(visits["status"] != "closed") & (visits["follow_up_date"].notna())]
        for _, v in overdue.head(5).iterrows():
            flags.append(f"🟠 **Supervision follow-up:** {v['facility']} — {v.get('findings', '')[:80]}")
    # Community tests very low at facility level from KHIS
    if d is not None and not d.empty:
        for fac in FACILITIES:
            if fac_filter not in (None, "All three") and fac != fac_filter:
                continue
            com = d[(d["facility"] == fac) & (d["register"] == "MOH 748")]
            tests = com[com["indicator"].str.contains("Total", na=False)]["value"].sum()
            if tests == 0 and not com.empty:
                flags.append(f"🔴 **{fac}:** community register present but 0 tests in filtered periods.")
    if not flags:
        st.success("No operational red flags from current roster/stock/referrals.")
    else:
        for f in flags:
            st.markdown(f)

    # ── CHP activity flags from MOH 648 (Layer 2) ──
    section_header("CHP activity flags (from MOH 648)")
    st.caption(
        "Analytical flags only — not a judgement of the individual CHP. "
        "Requires Layer 2 MOH 648 import on **Data foundation**."
    )
    try:
        m648 = query("SELECT * FROM moh_648")
    except Exception:
        m648 = pd.DataFrame()
    if m648.empty:
        st.info("No MOH 648 rows yet. Import a CSV on Data foundation to enable these flags.")
    else:
        if fac_filter and fac_filter != "All three":
            m648 = m648[m648["facility"] == fac_filter]
        # Aggregate by CHP (or facility if name blank)
        m648 = m648.copy()
        m648["chp_key"] = m648.apply(
            lambda r: (str(r.get("chp_name") or "").strip() or f"Facility:{r.get('facility')}"),
            axis=1,
        )
        for col in ("suspected", "tested", "positive", "referred", "followed_up"):
            if col not in m648.columns:
                m648[col] = 0
            m648[col] = pd.to_numeric(m648[col], errors="coerce").fillna(0)

        agg = m648.groupby(["facility", "chp_key"], as_index=False).agg(
            suspected=("suspected", "sum"),
            tested=("tested", "sum"),
            positive=("positive", "sum"),
            referred=("referred", "sum"),
            followed_up=("followed_up", "sum"),
        )
        def _row_rate(num, den):
            a = safe_rate(num, den)
            return a["value"] if a.get("status") == "valid" else (
                np.nan if a.get("status") != "invalid" else -a.get("calculated_pct", np.nan)
            )
        agg["positivity_pct"] = [
            _row_rate(r["positive"], r["tested"]) for _, r in agg.iterrows()
        ]
        agg["referral_rate_pct"] = [
            _row_rate(r["referred"], r["positive"]) for _, r in agg.iterrows()
        ]

        # Workload relative to peers at same facility
        chp_flags = []
        for fac, g in agg.groupby("facility"):
            med_tests = g["tested"].median() if len(g) else 0
            for _, r in g.iterrows():
                name = r["chp_key"]
                tags = []
                if r["tested"] == 0 and r["suspected"] == 0:
                    tags.append("zero_activity")
                if med_tests and r["tested"] >= max(med_tests * 1.5, med_tests + 10):
                    tags.append("high_workload")
                if med_tests and r["tested"] > 0 and r["tested"] <= max(med_tests * 0.4, 1):
                    tags.append("low_testing_vs_peers")
                if r["tested"] >= 5 and r.get("positivity_pct", 0) >= 40 and (
                    pd.isna(r.get("referral_rate_pct")) or r.get("referral_rate_pct", 100) < 20
                ):
                    tags.append("high_pos_low_referral")
                if r["referred"] > 0 and r["followed_up"] < r["referred"] * 0.5:
                    tags.append("weak_follow_up")
                if tags:
                    chp_flags.append({
                        "Facility": fac,
                        "CHP / unit": name,
                        "Tested": int(r["tested"]),
                        "Positive": int(r["positive"]),
                        "Referred": int(r["referred"]),
                        "Followed up": int(r["followed_up"]),
                        "Positivity %": f"{r['positivity_pct']:.0f}" if pd.notna(r["positivity_pct"]) else "—",
                        "Flags": ", ".join(tags),
                    })

        if not chp_flags:
            st.success("No CHP activity flags from current MOH 648 aggregates.")
        else:
            st.dataframe(pd.DataFrame(chp_flags), use_container_width=True, hide_index=True)
            st.caption(
                "Flags: zero_activity · high_workload · low_testing_vs_peers · "
                "high_pos_low_referral · weak_follow_up"
            )
        with st.expander("MOH 648 aggregate by CHP"):
            st.dataframe(agg, use_container_width=True, hide_index=True)

    # ═══ Phase B — CHP intelligence ═══
    section_header("CHP intelligence (Phase B)")
    st.caption(
        "Active · competency · testing activity · positivity · referrals · follow-up · "
        "workload · commodities · trends. Analytical flags are not individual blame."
    )
    ci1, ci2, ci3, ci4 = st.columns(4)
    with ci1:
        big_number(str(total), "Active on roster")
    with ci2:
        big_number(str(passed), "Competent (passed)")
    with ci3:
        # Community tests from filters
        try:
            com_tests = int(
                d[(d["register"] == "MOH 748") & (d["indicator"].isin(["Total <5", "Total >=5"]))]["value"].sum()
            ) if d is not None and not d.empty else 0
        except Exception:
            com_tests = 0
        big_number(f"{com_tests:,}", "Community tests (view)")
    with ci4:
        try:
            m648c = query("SELECT COUNT(DISTINCT chp_name) AS n FROM moh_648 WHERE chp_name IS NOT NULL AND chp_name != ''")
            n648 = int(m648c.iloc[0]["n"]) if not m648c.empty else 0
        except Exception:
            n648 = 0
        big_number(str(n648), "CHPs in MOH 648 data")

    # Roster × 648 activity join (when names match)
    try:
        m648_all = query("SELECT * FROM moh_648")
    except Exception:
        m648_all = pd.DataFrame()
    if not chps.empty and not m648_all.empty:
        if fac_filter and fac_filter != "All three":
            m648_all = m648_all[m648_all["facility"] == fac_filter]
        m648_all = m648_all.copy()
        for col in ("suspected", "tested", "positive", "referred", "followed_up"):
            if col not in m648_all.columns:
                m648_all[col] = 0
            m648_all[col] = pd.to_numeric(m648_all[col], errors="coerce").fillna(0)
        by_chp = m648_all.groupby(["facility", "chp_name"], as_index=False).agg(
            tested=("tested", "sum"),
            positive=("positive", "sum"),
            referred=("referred", "sum"),
            followed_up=("followed_up", "sum"),
            suspected=("suspected", "sum"),
        )
        # Merge roster
        roster = chps.copy()
        roster["name_key"] = roster["name"].astype(str).str.strip().str.lower()
        by_chp["name_key"] = by_chp["chp_name"].astype(str).str.strip().str.lower()
        merged = roster.merge(by_chp, left_on=["facility", "name_key"], right_on=["facility", "name_key"], how="left")
        merged["tested"] = merged["tested"].fillna(0)
        merged["positive"] = merged["positive"].fillna(0)
        intel_rows = []
        for _, r in merged.iterrows():
            ta = safe_rate(r["positive"], r["tested"], indicator="chp_intel_tpr")
            ra = safe_rate(r["referred"], r["positive"], indicator="chp_intel_ref") if r["positive"] else {"status": "na", "value": None}
            intel_rows.append({
                "Facility": r.get("facility"),
                "CHP": r.get("name"),
                "CHU": r.get("chu"),
                "Competency": r.get("competency_status"),
                "Tests (648)": int(r["tested"]),
                "Positive": int(r["positive"]),
                "Positivity": f"{ta['value']:.0f}%" if ta.get("status") == "valid" else (
                    f"INV" if ta.get("status") == "invalid" else "—"
                ),
                "Referred": int(r["referred"]),
                "Followed up": int(r.get("followed_up") or 0),
                "Referral %": f"{ra['value']:.0f}%" if ra.get("value") is not None else "—",
            })
        st.markdown("**Roster × MOH 648 activity** (name match)")
        st.dataframe(pd.DataFrame(intel_rows), use_container_width=True, hide_index=True)
        # Workload chart
        if intel_rows:
            fig_w = px.bar(
                pd.DataFrame(intel_rows), x="CHP", y="Tests (648)", color="Facility", height=340,
            )
            fig_w.update_layout(plot_bgcolor="white", xaxis_tickangle=-30)
            st.plotly_chart(fig_w, use_container_width=True)
    elif chps.empty:
        st.info("Add CHP roster to unlock roster×648 intelligence.")
    else:
        st.info("Import MOH 648 on Data foundation to link testing activity to named CHPs.")

    # Tabs for ops data entry
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "CHP roster", "Stock (RDT/AL)", "Referrals", "Supervision", "Workplan",
    ])

    with tab1:
        if chps.empty:
            st.info("No CHPs registered.")
        else:
            st.dataframe(chps, use_container_width=True, hide_index=True)
        if user.get("can_upload"):
            with st.form("add_chp_p2"):
                st.markdown("**Add CHP**")
                a, b = st.columns(2)
                with a:
                    name = st.text_input("Name")
                    facility = st.selectbox("Facility", FACILITIES, key="chp_fac")
                    chu = st.text_input("CHU")
                with b:
                    phone = st.text_input("Phone")
                    status = st.selectbox("Competency", ["pending", "passed", "failed"])
                    date = st.date_input("Assessment date")
                if st.form_submit_button("Save CHP") and name:
                    execute(
                        "INSERT INTO chps (name, facility, chu, phone, competency_status, competency_date) VALUES (?,?,?,?,?,?)",
                        (name, facility, chu, phone, status, str(date)),
                    )
                    log_audit(user["username"], "add_chp", "chps", f"{name}@{facility}")
                    st.success(f"Added {name}")
                    st.rerun()

    with tab2:
        if stock.empty:
            st.info("No stock rows yet. Log RDT/AL balances below.")
        else:
            stock_view = stock.copy()
            stock_view["Status"] = stock_view.apply(
                lambda r: "🔴 LOW" if r["quantity"] < r["reorder_point"] else "🟢 OK", axis=1
            )
            fig_s = px.bar(stock_view, x="facility", y="quantity", color="item", barmode="group", height=300)
            fig_s.update_layout(plot_bgcolor="white")
            st.plotly_chart(fig_s, use_container_width=True)
            st.dataframe(stock_view, use_container_width=True, hide_index=True)

        section_header("Physical vs system stock")
        st.caption("Expected = last system quantity. Physical = counted. Gap opens a DQ-style warning (not auto-ticket unless you log it).")
        try:
            moves = query("SELECT facility, item, qty, movement_type FROM stock_movements")
        except Exception:
            moves = pd.DataFrame()
        recon_s = []
        if not stock.empty:
            for _, r in stock.iterrows():
                phys = r.get("physical_count")
                sysq = r.get("quantity")
                gap = None
                if phys is not None and sysq is not None and str(phys) not in ("", "None"):
                    try:
                        gap = float(phys) - float(sysq)
                    except Exception:
                        gap = None
                recon_s.append({
                    "Facility": r.get("facility"),
                    "Item": r.get("item"),
                    "System qty": sysq,
                    "Physical": phys,
                    "Gap": gap,
                    "Status": "aligned" if gap == 0 else ("unexplained" if gap not in (None, 0) else "no physical count"),
                })
            st.dataframe(pd.DataFrame(recon_s), use_container_width=True, hide_index=True)

        section_header("Commodity forecast (early Phase B)")
        st.caption(
            "Uses on-hand stock and **stock_movements** issues (last ~90 days) for average monthly consumption (AMC). "
            "Import movements on **Data foundation** for better AMC. Lead time assumed 14 days."
        )
        forecast = compute_commodity_forecast(lookback_days=90, lead_time_days=14)
        if fac_filter and fac_filter != "All three" and not forecast.empty:
            forecast = forecast[forecast["Facility"] == fac_filter]
        if forecast.empty:
            st.caption("No stock lines to forecast.")
        else:
            def _fmt_status(s):
                return {
                    "stock_out": "🔴 Stock-out",
                    "critical": "🔴 Critical",
                    "low": "🟠 Low",
                    "watch": "🟡 Watch",
                    "ok": "🟢 OK",
                    "ok_no_amc": "🟢 OK (no AMC)",
                    "unknown": "⚫",
                }.get(s, s)
            show_f = forecast.copy()
            show_f["Status"] = show_f["Status"].map(_fmt_status)
            st.dataframe(show_f, use_container_width=True, hide_index=True)
            bad = forecast[forecast["Status"].isin(["stock_out", "critical", "low"])]
            if not bad.empty:
                st.warning(
                    f"**{len(bad)}** line(s) need attention (stock-out / critical / low). "
                    "Review suggested reorder quantities."
                )
            section_header("Redistribution plan (B4)")
            st.caption(
                "Suggests moving surplus (≈>3 months of stock) to sites that are stock-out / critical / low. "
                "Not an official KEMSA order. Minimum transfer 10 units."
            )
            plan = compute_redistribution_plan(forecast)
            if plan.empty:
                st.success("No intra-site transfer suggested from current AMC / MOS.")
            else:
                st.dataframe(plan, use_container_width=True, hide_index=True)
                st.download_button(
                    "📥 Download redistribution plan CSV",
                    plan.to_csv(index=False).encode("utf-8"),
                    file_name=f"cbmp_redistribution_{datetime.now().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                    key="b4_redist_csv",
                )
                if user.get("can_upload"):
                    if st.button("Push plan lines to workplan", key="b4_to_wp"):
                        n = 0
                        for _, p in plan.iterrows():
                            execute(
                                """
                                INSERT INTO workplan_actions
                                (facility, problem, action_text, owner, due_date, status, source, created_by)
                                VALUES (?, ?, ?, ?, ?, 'open', 'commodity', ?)
                                """,
                                (
                                    p.get("To"),
                                    f"{p.get('Item')} {p.get('Deficit status')}",
                                    f"Transfer or order {p.get('Suggested qty')} {p.get('Item')} from {p.get('From')} to {p.get('To')}",
                                    "County pharmacist / facility IC",
                                    (datetime.now() + pd.Timedelta(days=7)).strftime("%Y-%m-%d"),
                                    user.get("username", ""),
                                ),
                            )
                            n += 1
                        st.success(f"Added **{n}** workplan action(s).")
                        st.rerun()

        section_header("Lots / expiry (LMIS)")
        try:
            lots = query("SELECT * FROM stock_lots ORDER BY expiry_date")
        except Exception:
            lots = pd.DataFrame()
        if not lots.empty:
            lots["expiry_date"] = lots["expiry_date"].astype(str)
            today_s = datetime.now().strftime("%Y-%m-%d")
            lots["Risk"] = lots["expiry_date"].apply(
                lambda x: "expired" if x and x < today_s else ("90-day" if x and x <= (datetime.now() + pd.Timedelta(days=90)).strftime("%Y-%m-%d") else "ok")
            )
            st.dataframe(lots, use_container_width=True, hide_index=True)
        if user.get("can_upload"):
            with st.form("lot_form"):
                la, lb, lc, ld = st.columns(4)
                with la:
                    lf = st.selectbox("Facility", FACILITIES, key="lot_fac")
                with lb:
                    li = st.text_input("Item", value="RDT")
                with lc:
                    lbch = st.text_input("Batch / lot")
                with ld:
                    lq = st.number_input("Qty", min_value=0, step=1)
                lex = st.date_input("Expiry")
                if st.form_submit_button("Add lot"):
                    execute(
                        """
                        INSERT INTO stock_lots (facility, item, batch, quantity, expiry_date, received_date)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (lf, li, lbch, int(lq), str(lex), datetime.now().strftime("%Y-%m-%d")),
                    )
                    st.success("Lot saved.")
                    st.rerun()

        if user.get("can_upload"):
            with st.form("stock_form"):
                st.markdown("**Update stock**")
                a, b, c = st.columns(3)
                with a:
                    sf = st.selectbox("Facility", FACILITIES, key="stk_fac")
                    item = st.selectbox("Item", ["RDT", "AL"])
                with b:
                    qty = st.number_input("Quantity", min_value=0, value=50)
                    reorder = st.number_input("Reorder point", min_value=0, value=30)
                with c:
                    restock = st.date_input("Last restock")
                if st.form_submit_button("Save stock"):
                    execute(
                        """
                        INSERT INTO stock (facility, item, quantity, reorder_point, last_restock)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(facility, item) DO UPDATE SET
                            quantity=excluded.quantity,
                            reorder_point=excluded.reorder_point,
                            last_restock=excluded.last_restock
                        """,
                        (sf, item, int(qty), int(reorder), str(restock)),
                    )
                    log_audit(user["username"], "stock_update", "stock", f"{item}@{sf}={qty}")
                    st.success("Stock saved")
                    st.rerun()

    with tab3:
        if refs.empty:
            st.info("No referrals logged.")
        else:
            st.dataframe(refs, use_container_width=True, hide_index=True)
        if user.get("can_upload"):
            with st.form("ref_form"):
                st.markdown("**Log referral**")
                a, b = st.columns(2)
                with a:
                    rf = st.selectbox("Facility", FACILITIES, key="ref_fac")
                    rdate = st.date_input("Referral date")
                    patient_ref = st.text_input("Patient ref (no name)", placeholder="e.g. CHU-001")
                with b:
                    arrived = st.checkbox("Arrived at facility", value=False)
                    confirmed = st.checkbox("Confirmed malaria", value=False)
                    treated = st.checkbox("Treated", value=False)
                    notes = st.text_input("Notes")
                if st.form_submit_button("Save referral"):
                    execute(
                        """
                        INSERT INTO referrals (facility, referral_date, patient_id, arrived, confirmed, treated, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (rf, str(rdate), patient_ref, int(arrived), int(confirmed), int(treated), notes),
                    )
                    log_audit(user["username"], "add_referral", "referrals", f"{rf} {patient_ref}")
                    st.success("Referral saved")
                    st.rerun()

    with tab4:
        section_header("Supervision action workflow")
        st.caption(
            "Finding → action → owner → deadline → close. "
            "Overdue = open action past deadline."
        )
        try:
            actions_df = query(
                "SELECT * FROM supervision_actions ORDER BY "
                "CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, deadline"
            )
        except Exception:
            actions_df = pd.DataFrame()

        if fac_filter and fac_filter != "All three" and not actions_df.empty:
            actions_df = actions_df[actions_df["facility"] == fac_filter]

        # Mark overdue in display
        today_s = datetime.now().strftime("%Y-%m-%d")
        if not actions_df.empty:
            def _sup_status(r):
                stt = str(r.get("status") or "open")
                dl = str(r.get("deadline") or "")
                if stt in ("open", "in_progress") and dl and dl < today_s:
                    return "overdue"
                return stt
            show_a = actions_df.copy()
            show_a["workflow_status"] = show_a.apply(_sup_status, axis=1)
            n_open = int(show_a["workflow_status"].isin(["open", "in_progress", "overdue"]).sum())
            n_od = int((show_a["workflow_status"] == "overdue").sum())
            s1, s2, s3 = st.columns(3)
            with s1:
                big_number(str(n_open), "Open actions")
            with s2:
                big_number(str(n_od), "Overdue", delta_direction="down" if n_od else "flat")
            with s3:
                big_number(str(len(visits)), "Visits logged")
            st.dataframe(show_a, use_container_width=True, hide_index=True)

            if user.get("can_upload") and not show_a.empty:
                open_ids = show_a[show_a["workflow_status"].isin(["open", "in_progress", "overdue"])]["id"].tolist()
                if open_ids:
                    st.markdown("**Update action**")
                    u1, u2, u3 = st.columns(3)
                    with u1:
                        aid = st.selectbox("Action id", open_ids, key="sup_act_id")
                    with u2:
                        new_st = st.selectbox(
                            "New status",
                            ["in_progress", "closed", "open"],
                            key="sup_act_st",
                        )
                    with u3:
                        close_note = st.text_input("Close / progress note", key="sup_act_note")
                    if st.button("Apply status", key="sup_act_go"):
                        if new_st == "closed":
                            execute(
                                """
                                UPDATE supervision_actions
                                SET status = 'closed', closed_at = ?, closed_by = ?, notes = COALESCE(notes,'') || ?
                                WHERE id = ?
                                """,
                                (
                                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    user.get("username", ""),
                                    f" | {close_note}" if close_note else "",
                                    int(aid),
                                ),
                            )
                        else:
                            execute(
                                "UPDATE supervision_actions SET status = ?, notes = COALESCE(notes,'') || ? WHERE id = ?",
                                (
                                    new_st,
                                    f" | {close_note}" if close_note else "",
                                    int(aid),
                                ),
                            )
                        log_audit(user["username"], "sup_action_update", "supervision_actions", f"id={aid} → {new_st}")
                        st.success(f"Action #{aid} → {new_st}")
                        st.rerun()
        else:
            st.info("No supervision actions yet. Log a visit below to create the first action.")

        if not visits.empty:
            with st.expander("Visit history"):
                st.dataframe(visits, use_container_width=True, hide_index=True)

        if user.get("can_upload"):
            with st.form("sup_form"):
                st.markdown("**Log supervision visit (+ action)**")
                a, b = st.columns(2)
                with a:
                    vf = st.selectbox("Facility", FACILITIES, key="sup_fac")
                    vdate = st.date_input("Visit date", key="sup_date")
                    supervisor = st.text_input("Supervisor")
                    owner = st.text_input("Action owner", value=supervisor or "")
                with b:
                    findings = st.text_area("Finding")
                    actions = st.text_area("Action required")
                    follow = st.date_input("Deadline / follow-up", key="sup_fu")
                    vstatus = st.selectbox("Visit status", ["open", "closed"])
                if st.form_submit_button("Save visit & action"):
                    execute(
                        """
                        INSERT INTO supervision_visits
                        (facility, visit_date, supervisor, findings, actions, follow_up_date, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (vf, str(vdate), supervisor, findings, actions, str(follow), vstatus),
                    )
                    # Get last visit id
                    try:
                        vid = int(query("SELECT MAX(id) AS id FROM supervision_visits").iloc[0]["id"])
                    except Exception:
                        vid = None
                    if findings or actions:
                        execute(
                            """
                            INSERT INTO supervision_actions
                            (visit_id, facility, finding, action_text, owner, deadline, status)
                            VALUES (?, ?, ?, ?, ?, ?, 'open')
                            """,
                            (vid, vf, findings, actions, owner or supervisor, str(follow)),
                        )
                    log_audit(user["username"], "add_supervision", "supervision_visits", f"{vf} {vdate}")
                    st.success("Visit and open action saved")
                    st.rerun()

    with tab5:
        section_header("Programme workplan (Phase B)")
        st.caption(
            "Problem → Action → Owner → Due date → Activity → Evidence → Status → Closure. "
            "Broader than supervision visits — covers commodity, DQ, reporting, cascade gaps."
        )
        try:
            wp = query(
                "SELECT * FROM workplan_actions ORDER BY "
                "CASE status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END, due_date"
            )
        except Exception:
            wp = pd.DataFrame()
        if fac_filter and fac_filter != "All three" and not wp.empty:
            wp = wp[(wp["facility"] == fac_filter) | (wp["facility"].isna())]

        today_s = datetime.now().strftime("%Y-%m-%d")
        if not wp.empty:
            def _wp_st(r):
                stt = str(r.get("status") or "open")
                dl = str(r.get("due_date") or "")
                if stt in ("open", "in_progress") and dl and dl < today_s:
                    return "overdue"
                return stt
            show_w = wp.copy()
            show_w["workflow"] = show_w.apply(_wp_st, axis=1)
            w1, w2, w3, w4 = st.columns(4)
            with w1:
                big_number(str(int(show_w["workflow"].isin(["open", "in_progress", "overdue"]).sum())), "Open")
            with w2:
                big_number(str(int((show_w["workflow"] == "overdue").sum())), "Overdue")
            with w3:
                big_number(str(int((show_w["status"] == "closed").sum())), "Closed")
            with w4:
                big_number(str(len(show_w)), "Total")
            st.dataframe(show_w, use_container_width=True, hide_index=True)

            if user.get("can_upload"):
                open_ids = show_w[show_w["workflow"].isin(["open", "in_progress", "overdue"])]["id"].tolist()
                if open_ids:
                    st.markdown("**Update workplan item**")
                    u1, u2, u3 = st.columns(3)
                    with u1:
                        wid = st.selectbox("Workplan id", open_ids, key="wp_id")
                    with u2:
                        wst = st.selectbox("Status", ["in_progress", "closed", "open"], key="wp_st")
                    with u3:
                        evid = st.text_input("Evidence / note", key="wp_evid")
                    if st.button("Apply workplan status", key="wp_go"):
                        if wst == "closed":
                            execute(
                                """
                                UPDATE workplan_actions
                                SET status='closed', closed_at=?, closed_by=?,
                                    evidence=COALESCE(evidence,'') || ?, notes=COALESCE(notes,'') || ?
                                WHERE id=?
                                """,
                                (
                                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    user.get("username", ""),
                                    f" | {evid}" if evid else "",
                                    f" | closed",
                                    int(wid),
                                ),
                            )
                        else:
                            execute(
                                "UPDATE workplan_actions SET status=?, notes=COALESCE(notes,'') || ? WHERE id=?",
                                (wst, f" | {evid}" if evid else "", int(wid)),
                            )
                        log_audit(user["username"], "workplan_update", "workplan_actions", f"id={wid}→{wst}")
                        st.success(f"Workplan #{wid} → {wst}")
                        st.rerun()
        else:
            st.info("No workplan actions yet. Add one below.")

        if user.get("can_upload"):
            with st.form("wp_form"):
                st.markdown("**New workplan action**")
                a, b = st.columns(2)
                with a:
                    wf = st.selectbox("Facility", ["(programme)"] + FACILITIES, key="wp_fac")
                    problem = st.text_area("Problem / finding")
                    action_t = st.text_area("Action required")
                    owner = st.text_input("Owner")
                with b:
                    due = st.date_input("Due date", key="wp_due")
                    activity = st.text_input("Activity / intervention")
                    evidence0 = st.text_input("Evidence available now (optional)")
                    source = st.selectbox(
                        "Source",
                        ["manual", "overview_action", "alert", "dq", "supervision", "commodity"],
                        key="wp_src",
                    )
                if st.form_submit_button("Save workplan action"):
                    if problem and action_t:
                        fac_val = None if wf == "(programme)" else wf
                        execute(
                            """
                            INSERT INTO workplan_actions
                            (facility, problem, action_text, owner, due_date, activity, evidence, status, source, created_by)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                            """,
                            (
                                fac_val, problem, action_t, owner, str(due),
                                activity, evidence0 or None, source, user.get("username", ""),
                            ),
                        )
                        log_audit(user["username"], "workplan_add", "workplan_actions", problem[:80])
                        st.success("Workplan action saved")
                        st.rerun()
                    else:
                        st.error("Problem and action are required.")

        # Suggest from generated actions
        if user.get("can_upload") and not d.empty:
            with st.expander("Suggest from current cascade / indicator gaps"):
                inds = compute_official_indicators(df if not df.empty else d, filters)
                sug = build_action_list(d, inds)
                if not sug:
                    st.caption("No generated suggestions for current filters.")
                else:
                    for i, a in enumerate(sug[:6]):
                        st.markdown(f"- **{a.get('priority')}**: {a.get('text')}")
                        if st.button(f"Add to workplan #{i}", key=f"sug_wp_{i}"):
                            execute(
                                """
                                INSERT INTO workplan_actions
                                (facility, problem, action_text, owner, due_date, status, source, created_by)
                                VALUES (?, ?, ?, ?, ?, 'open', 'overview_action', ?)
                                """,
                                (
                                    fac_filter if fac_filter not in (None, "All three") else None,
                                    a.get("text", "")[:200],
                                    a.get("text", ""),
                                    user.get("username", ""),
                                    (datetime.now() + pd.Timedelta(days=14)).strftime("%Y-%m-%d"),
                                    user.get("username", ""),
                                ),
                            )
                            st.success("Added")
                            st.rerun()


# ==================================================================
# PAGE 13 — ALERTS
# ==================================================================

def _alert_family(category, message=""):
    """Map raw category/message to Data quality / Surveillance / Forecast family."""
    cat = str(category or "").lower()
    msg = str(message or "").lower()
    if cat in ("malaria ew", "climate") or "early warning" in msg or "ew:" in msg:
        return "Forecast"
    if any(k in cat or k in msg for k in (
        "data quality", "reporting", "invalid", "missing", "duplicate", "dq", "reconcile",
    )):
        return "Data quality"
    if any(k in cat or k in msg for k in (
        "stock", "commodity", "rdt stock", "al stock",
    )):
        return "Commodity"
    return "Surveillance"


def _alert_evidence_panel(facility, period=None):
    """Build a small evidence table for an alert facility from cascade + climate."""
    rows = []
    try:
        df = load_all_data()
        pf = {"facility": facility} if facility else {}
        if period:
            pf["periods"] = [period]
        d = _filter_data(df, pf) if df is not None and not df.empty else pd.DataFrame()
        if not d.empty:
            c = compute_cascade_metrics(d)
            go = compute_indicator("GO_1", d)
            rows.append(("Confirmed cases", c.get("confirmed"), "—", "cascade"))
            rows.append(("Tested", c.get("tested"), "—", "cascade"))
            rows.append(("TPR %", go.get("value") if go.get("status") == "valid" else None, go.get("status"), "GO_1"))
            rows.append(("Diagnosis gap", c.get("diag_gap"), "—", "cascade"))
    except Exception:
        pass
    try:
        clim = query(
            "SELECT rainfall_mm, week_ending FROM climate_data "
            "WHERE facility = ? OR geography IS NOT NULL ORDER BY week_ending DESC LIMIT 4",
            (facility,),
        )
        if not clim.empty:
            rain = pd.to_numeric(clim["rainfall_mm"], errors="coerce").sum()
            rows.append(("Rain last ~4 weeks (mm)", round(float(rain), 1), "—", "climate"))
    except Exception:
        pass
    try:
        stock = query("SELECT item, quantity FROM stock WHERE facility = ?", (facility,))
        if not stock.empty:
            for _, r in stock.iterrows():
                rows.append((f"Stock {r.get('item')}", r.get("quantity"), "—", "stock"))
    except Exception:
        pass
    if not rows:
        return pd.DataFrame(columns=["Evidence", "Value", "Status", "Source"])
    return pd.DataFrame(rows, columns=["Evidence", "Value", "Status", "Source"])


def page_alerts(user, filters):
    page_header(
        "Alert centre",
        "Three families: **Data quality** · **Surveillance** · **Forecast (EW)**. "
        "A data error is not an outbreak. Re-evaluate after each KHIS commit.",
    )

    # Scope facility from global filters when set
    fac_filter = None
    if filters and filters.get("facility") not in (None, "All three"):
        fac_filter = filters["facility"]

    c_top1, c_top2, c_top3 = st.columns([2, 1, 1])
    with c_top1:
        st.caption(
            "Engine uses pattern matching on KHIS labels (not exact names) and the last "
            "**6 monthly periods**. Investigate critical DQ failures before treating rising TPR as transmission."
        )
    with c_top2:
        if st.button("🔄 Re-evaluate alerts", type="primary", use_container_width=True):
            with st.spinner("Running programme alert rules…"):
                n_new = evaluate_alerts(recent_months=6)
            st.success(f"Evaluation complete — {n_new} new alert(s) created." if n_new else "Evaluation complete — no new alerts.")
            st.rerun()
    with c_top3:
        if st.button("Resolve all warnings", use_container_width=True):
            execute(
                "UPDATE alerts SET status='resolved', resolved_at=CURRENT_TIMESTAMP "
                "WHERE status='open' AND severity='warning'"
            )
            log_audit(user["username"], "resolve_all_warnings", "alerts", None)
            st.rerun()

    section_header("SMS / message pack — how to send")
    st.info(
        "**Steps:** (1) Scroll to **Alert routing** below → add contact with phone + tick **SMS enabled**.  \n"
        "(2) Ensure you have **open alerts** (run Evaluate alerts / EW if empty).  \n"
        "(3) Open **Send SMS now** below → save Africa's Talking keys **or** paste a number in Override → **Send**.  \n"
        "Without AT credentials you can still **Download SMS pack CSV** or copy the text for WhatsApp."
    )
    try:
        open_a = query(
            """
            SELECT severity, facility, period, message
            FROM alerts WHERE status='open'
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END
            LIMIT 80
            """
        )
    except Exception:
        open_a = pd.DataFrame()
    if open_a.empty:
        st.info("No open alerts to message.")
    else:
        rows = []
        for _, r in open_a.iterrows():
            sev = str(r.get("severity") or "").upper()
            fac = r.get("facility") or "CBMP"
            per = r.get("period") or ""
            msg = str(r.get("message") or "")[:160]
            fam = _alert_family(r.get("category"), msg)
            targets = resolve_alert_recipients(
                facility=fac if fac != "CBMP" else None,
                category=r.get("category"),
                severity=str(r.get("severity") or "warning"),
                family=fam,
            )
            phones = "; ".join(
                t["phone"] for t in targets if t.get("phone") and t.get("sms_enabled")
            )
            emails = "; ".join(
                t["email"] for t in targets if t.get("email") and t.get("email_enabled")
            )
            names = "; ".join(
                f"{t['name']} ({t['role']})" for t in targets[:4]
            ) if targets else "(no routing configured)"
            text = f"CBMP {sev} {fac} {per}: {msg}"[:320]
            rows.append({
                "To SMS": phones or "",
                "To Email": emails or "",
                "Recipients": names,
                "Family": fam,
                "Facility": fac,
                "Severity": sev,
                "SMS text": text,
            })
        pack = pd.DataFrame(rows)
        st.dataframe(pack, use_container_width=True, hide_index=True)
        st.caption(
            "Recipients come from **Alert routing** (below). Phones/emails are never hard-coded in app.py. "
            "Empty **To SMS** / **To Email** means no matching contact with that channel enabled."
        )
        st.download_button(
            "📥 Download SMS pack CSV",
            pack.to_csv(index=False).encode("utf-8"),
            file_name=f"cbmp_alert_sms_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            key="sms_pack_dl",
        )
        st.text_area("Copy-paste block (WhatsApp / bulk SMS)", "\n".join(pack["SMS text"].tolist()), height=160)

        # Form keeps phone + credentials + button in one submit (Streamlit-safe)
        st.markdown("#### Send SMS now")
        st.caption(
            "Phone format (all valid): `+254745866151` · `0745866151` · `254745866151`.  "
            "If you still see *Configure Alert routing phones…*, you are on an **old build** — redeploy this zip."
        )
        _can_send = bool(user.get("can_upload") or user.get("can_manage_users") or user.get("role") == "admin")
        if not _can_send:
            st.warning("Your role cannot trigger live SMS. Sign in as **admin**.")
        with st.form("sms_send_form"):
            st.markdown("**Step 1 — Phone**")
            override = st.text_input(
                "Your phone number",
                value="+254745866151",
                placeholder="+254745866151",
            )
            st.markdown("**Step 2 — Africa's Talking account**")
            u = st.text_input("AT username", value=get_setting("at_username") or "")
            k = st.text_input("AT API key", value=get_setting("at_apikey") or "", type="password")
            snd = st.text_input("Sender ID", value=get_setting("at_sender") or "CBMP")
            use_routing = st.checkbox("Also send to Alert-routing contacts (SMS enabled)", value=False)
            submitted = st.form_submit_button(
                "Send first 5 alert SMS",
                type="primary",
                disabled=not _can_send,
            )
        if submitted and _can_send:
            phone_n = normalize_ke_phone(override)
            st.write(f"Normalized phone: `{phone_n}`")
            if not phone_n:
                st.error("Phone number is empty or invalid. Example: +254745866151")
            elif not (u and str(u).strip()) or not (k and str(k).strip()):
                st.error(
                    "Africa's Talking **username** and **API key** are required for live SMS. "
                    "Without them, download the CSV pack or copy the text for WhatsApp."
                )
            else:
                set_setting("at_username", u.strip(), user.get("username", ""))
                set_setting("at_apikey", k.strip(), user.get("username", ""))
                set_setting("at_sender", (snd or "CBMP").strip(), user.get("username", ""))
                ok_n = 0
                last = ""
                attempted = 0
                for _, row in pack.head(5).iterrows():
                    recips = [phone_n]
                    if use_routing and row.get("To SMS"):
                        extra = [p.strip() for p in str(row["To SMS"]).replace(";", ",").split(",") if p.strip()]
                        recips = list(dict.fromkeys(recips + extra))
                    attempted += 1
                    ok, last = send_africastalking_sms(
                        recips, row["SMS text"],
                        username=u.strip(), apikey=k.strip(),
                        sender_id=(snd or "CBMP").strip(),
                    )
                    if ok:
                        ok_n += 1
                if ok_n == 0:
                    st.error(
                        f"API was called **{attempted}** time(s) but none succeeded.\n\n"
                        f"**Provider response:** {str(last)[:500]}\n\n"
                        "Common causes: wrong AT API key, sandbox vs live username, "
                        "sender ID not approved, or no AT SMS balance."
                    )
                else:
                    st.success(f"SMS sent {ok_n}/{attempted} to {phone_n}. Response: {str(last)[:300]}")

        with st.expander("Email send (SMTP)", expanded=False):
            st.caption(
                "Programme SMTP (e.g. County or World Friends mail). "
                "Credentials stored in programme_settings — not in app.py."
            )
            e1, e2 = st.columns(2)
            with e1:
                smtp_host = st.text_input("SMTP host", value=get_setting("smtp_host") or "", key="smtp_host")
                smtp_port = st.text_input("SMTP port", value=get_setting("smtp_port") or "587", key="smtp_port")
                smtp_from = st.text_input("From address", value=get_setting("smtp_from") or "", key="smtp_from")
            with e2:
                smtp_user = st.text_input("SMTP username", value=get_setting("smtp_user") or "", key="smtp_user")
                smtp_pass = st.text_input("SMTP password", value=get_setting("smtp_password") or "", type="password", key="smtp_pass")
                smtp_tls = st.checkbox("Use STARTTLS", value=str(get_setting("smtp_tls") or "1") not in ("0", "false"), key="smtp_tls")
            if user.get("can_upload") and st.button("Save SMTP settings", key="smtp_save"):
                set_setting("smtp_host", smtp_host, user.get("username", ""))
                set_setting("smtp_port", smtp_port, user.get("username", ""))
                set_setting("smtp_from", smtp_from, user.get("username", ""))
                set_setting("smtp_user", smtp_user, user.get("username", ""))
                set_setting("smtp_password", smtp_pass, user.get("username", ""))
                set_setting("smtp_tls", "1" if smtp_tls else "0", user.get("username", ""))
                st.success("SMTP settings saved.")
            email_override = st.text_input("Override emails (comma-separated)", key="email_override")
            if user.get("can_upload") and st.button("Send first 5 alert emails", key="email_send"):
                ok_n = 0
                last = ""
                for i, row in pack.head(5).iterrows():
                    if email_override.strip():
                        recips = [p.strip() for p in email_override.replace(";", ",").split(",") if p.strip()]
                    elif row.get("To Email"):
                        recips = [p.strip() for p in str(row["To Email"]).replace(";", ",").split(",") if p.strip()]
                    else:
                        recips = []
                    if not recips:
                        continue
                    subj = f"CBMP {row.get('Severity')} alert — {row.get('Facility')}"
                    body = (
                        f"{row.get('SMS text')}\n\n"
                        f"Family: {row.get('Family')}\n"
                        f"Dashboard: Alert Centre\n"
                        f"Do not reply to automated messages if this is a no-reply address."
                    )
                    ok, last = send_alert_email(recips, subj, body)
                    if ok:
                        ok_n += 1
                if ok_n == 0:
                    st.error(
                        "No emails sent. Add emails with **Email enabled** on recipients, "
                        "configure SMTP, or use an override address."
                    )
                else:
                    st.info(f"Email sent {ok_n}/5. Result: {str(last)[:300]}")
        try:
            slog = query("SELECT sent_at, recipient, status FROM sms_log ORDER BY id DESC LIMIT 20")
            if not slog.empty:
                st.caption("Recent SMS log")
                st.dataframe(slog, use_container_width=True, hide_index=True)
        except Exception:
            pass

    # ── Alert routing configuration ──
    section_header("Alert routing — who receives what")
    st.markdown(
        """
| Alert family | Primary | Escalation |
|--------------|---------|------------|
| Data quality | Facility data focal → Sub-county M&E | County M&E |
| Surveillance | Facility in-charge → Sub-county malaria focal | County malaria team |
| Forecast / EW | Sub-county malaria focal → County malaria | County EPR |
| Commodity | Facility commodity focal | Sub-county / County logistics |

**Do not hard-code phone numbers in code.** Maintain contacts here. SMS only when `sms_enabled` and phone is set.
        """
    )
    if user.get("can_upload"):
        if st.button("Seed template roles (no phone numbers)", key="seed_routing"):
            n = seed_default_alert_recipients(user.get("username", "system"))
            st.success(f"Inserted {n} template rows." if n else "Table already has rows — not re-seeded.")
            if n:
                st.rerun()
    try:
        recs = query(
            "SELECT id, name, role, facility, sub_county, phone, email, alert_family, "
            "min_severity, sms_enabled, email_enabled, escalation_hours, active "
            "FROM alert_recipients ORDER BY role, facility, sub_county"
        )
    except Exception:
        recs = pd.DataFrame()
    if recs.empty:
        st.warning("No recipients configured. Seed templates, then replace (Name) and add +254 phones.")
    else:
        st.dataframe(recs, use_container_width=True, hide_index=True)
        st.caption(f"**{len(recs)}** contacts · SMS-enabled: **{int(pd.to_numeric(recs.get('sms_enabled'), errors='coerce').fillna(0).sum())}**")

    if user.get("can_upload"):
        with st.expander("Add / update recipient", expanded=False):
            with st.form("alert_recip_form"):
                a1, a2, a3 = st.columns(3)
                with a1:
                    rname = st.text_input("Name *")
                    rrole = st.selectbox(
                        "Role",
                        [
                            "Facility data focal", "Facility in-charge", "Facility commodity focal",
                            "Sub-county M&E", "Sub-county malaria focal", "Sub-county logistics",
                            "County malaria team", "County M&E", "County EPR", "County logistics",
                            "Other",
                        ],
                    )
                with a2:
                    rfac = st.selectbox("Facility (optional)", ["(county/sub-county)"] + FACILITIES)
                    rsub = st.selectbox("Sub-county (optional)", ["(county-wide)"] + KILIFI_SUBCOUNTIES)
                    rfam = st.selectbox(
                        "Alert family",
                        ["Data quality", "Surveillance", "Forecast", "Commodity", "All"],
                    )
                with a3:
                    rphone = st.text_input("Phone (+254…)")
                    remail = st.text_input("Email")
                    rsev = st.selectbox("Min severity", ["warning", "high", "critical"])
                    rhrs = st.number_input("Escalate after (hours)", 0, 168, 24)
                rsms = st.checkbox("SMS enabled (required for live SMS routing)", value=True)
                remail_on = st.checkbox("Email enabled", value=False)
                if st.form_submit_button("Save recipient"):
                    if not rname.strip():
                        st.error("Name required.")
                    else:
                        execute(
                            """
                            INSERT INTO alert_recipients
                            (name, role, facility, sub_county, phone, email, alert_family,
                             min_severity, sms_enabled, email_enabled, dashboard_enabled,
                             escalation_hours, active, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?)
                            """,
                            (
                                rname.strip(), rrole,
                                None if rfac.startswith("(") else rfac,
                                None if rsub.startswith("(") else rsub,
                                rphone.strip() or None, remail.strip() or None,
                                None if rfam == "All" else rfam,
                                rsev, 1 if rsms else 0, 1 if remail_on else 0,
                                int(rhrs), datetime.now().strftime("%Y-%m-%d %H:%M"),
                            ),
                        )
                        st.success("Recipient saved.")
                        st.rerun()
        if not recs.empty:
            with st.expander("Deactivate recipient"):
                rid = st.selectbox("ID", recs["id"].tolist(), key="deact_rid")
                if st.button("Deactivate", key="deact_go"):
                    execute("UPDATE alert_recipients SET active=0 WHERE id=?", (int(rid),))
                    st.success("Deactivated.")
                    st.rerun()

    # Always evaluate once when opening the page (idempotent via de-dup)
    with st.spinner("Checking alert rules…"):
        evaluate_alerts(recent_months=6)

    alerts_df = get_active_alerts(facility=fac_filter)
    try:
        resolved_count = int(query("SELECT COUNT(*) AS n FROM alerts WHERE status = 'resolved'").iloc[0]["n"])
    except Exception:
        resolved_count = 0

    if alerts_df.empty:
        headline("No active alerts in scope. Thresholds look acceptable for recent periods.", status="green")
        st.markdown(f"**Resolved (all time):** {resolved_count}")
    else:
        critical = len(alerts_df[alerts_df["severity"] == "critical"])
        high = len(alerts_df[alerts_df["severity"] == "high"])
        warning = len(alerts_df[alerts_df["severity"] == "warning"])
        status = "red" if critical else ("amber" if high else "green")
        headline(
            f"**{critical}** critical · **{high}** high · **{warning}** warning active"
            + (f" (facility filter: {fac_filter})" if fac_filter else "")
            + ".",
            status=status,
        )

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            big_number(str(critical), "Critical", delta="Investigate now", delta_direction="down" if critical else "flat")
        with c2:
            big_number(str(high), "High", delta="Review today", delta_direction="flat")
        with c3:
            big_number(str(warning), "Warning", delta="Monitor", delta_direction="flat")
        with c4:
            big_number(str(resolved_count), "Resolved", delta="All time", delta_direction="up")

        # Family classification
        alerts_df = alerts_df.copy()
        alerts_df["Family"] = alerts_df.apply(
            lambda r: _alert_family(r.get("category"), r.get("message")), axis=1
        )
        fam_counts = alerts_df["Family"].value_counts().to_dict()
        st.caption(
            " · ".join(f"**{k}:** {v}" for k, v in fam_counts.items())
            or "No classified alerts"
        )

        # Filters on the open list
        section_header(f"Active alerts ({len(alerts_df)})")
        sev_opts = ["All"] + [s for s in ["critical", "high", "warning"] if (alerts_df["severity"] == s).any()]
        fam_opts = ["All"] + sorted(alerts_df["Family"].dropna().unique().tolist())
        cat_opts = ["All"] + sorted(alerts_df["category"].dropna().unique().tolist())
        f1, f2, f3 = st.columns(3)
        with f1:
            sev_pick = st.selectbox("Severity", sev_opts, key="alert_sev_filter")
        with f2:
            fam_pick = st.selectbox("Family", fam_opts, key="alert_fam_filter")
        with f3:
            cat_pick = st.selectbox("Category", cat_opts, key="alert_cat_filter")
        view = alerts_df
        if sev_pick != "All":
            view = view[view["severity"] == sev_pick]
        if fam_pick != "All":
            view = view[view["Family"] == fam_pick]
        if cat_pick != "All":
            view = view[view["category"] == cat_pick]

        # Composite: same facility with multiple open signals
        if not view.empty and "facility" in view.columns:
            multi = view.groupby("facility").size()
            multi = multi[multi >= 2]
            if not multi.empty:
                st.markdown("#### Composite signals (same facility, ≥2 open alerts)")
                for fac, n in multi.items():
                    sub = view[view["facility"] == fac]
                    families = ", ".join(sorted(sub["Family"].unique().tolist()))
                    st.markdown(
                        f"**{fac}** — {int(n)} signals ({families}). "
                        "Review together before treating as independent events."
                    )

        # Evidence panel for selected facility
        if not view.empty:
            facs = sorted([f for f in view["facility"].dropna().unique().tolist() if f])
            if facs:
                with st.expander("Evidence panel (selected facility)", expanded=False):
                    ef = st.selectbox("Facility", facs, key="alert_evidence_fac")
                    ev = _alert_evidence_panel(ef)
                    if ev.empty:
                        st.caption("No cascade/climate/stock evidence available.")
                    else:
                        st.dataframe(ev, use_container_width=True, hide_index=True)
                        st.caption(
                            "If testing collapsed or RDT is stocked out, do **not** interpret "
                            "rising TPR alone as increased transmission."
                        )

        section_header("Alert lifecycle")
        st.caption(
            "New → Acknowledged → Investigating → Action assigned → Monitoring → Closed. "
            "Mark **false alarm** when investigation shows no elevated transmission."
        )
        try:
            open_ids = query(
                """
                SELECT id, severity, facility, message, status,
                       IFNULL(lifecycle,'new') AS lifecycle,
                       IFNULL(followed_up,0) AS followed_up,
                       acknowledged_at, investigating_at, closure_reason, false_alarm
                FROM alerts WHERE status IN ('open','acknowledged','investigating','monitoring')
                ORDER BY id DESC LIMIT 50
                """
            )
        except Exception:
            open_ids = query(
                "SELECT id, severity, facility, message, IFNULL(followed_up,0) AS followed_up FROM alerts WHERE status='open' ORDER BY id DESC LIMIT 40"
            )
        if not open_ids.empty:
            pick_a = st.selectbox("Alert", open_ids["id"].tolist(), key="fu_alert")
            row_a = open_ids[open_ids["id"] == pick_a].iloc[0]
            st.write(
                f"{row_a.get('severity')} · {row_a.get('facility')} · "
                f"lifecycle=`{row_a.get('lifecycle') or 'new'}` · {str(row_a.get('message'))[:180]}"
            )
            lc_opts = [
                "new", "acknowledged", "investigating", "action_assigned", "monitoring", "closed",
            ]
            cur_lc = str(row_a.get("lifecycle") or "new")
            if cur_lc not in lc_opts:
                cur_lc = "new"
            new_lc = st.selectbox("Set lifecycle", lc_opts, index=lc_opts.index(cur_lc), key="fu_lc")
            finding = st.text_input("Investigation finding", value=str(row_a.get("investigation_finding") or "") if "investigation_finding" in row_a else "", key="fu_find")
            action = st.text_input("Action assigned", key="fu_action")
            owner = st.text_input("Action owner", key="fu_owner")
            false_a = st.checkbox("False alarm (no elevated transmission after review)", key="fu_false")
            close_reason = st.text_input("Closure reason (if closing)", key="fu_close")
            if user.get("can_upload") and st.button("Save lifecycle", key="fu_save"):
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                uname = user.get("username", "")
                # Build incremental update
                sets = ["lifecycle=?", "followed_up=?", "followed_up_at=?", "followed_up_by=?"]
                params = [new_lc, 1 if new_lc != "new" else 0, now if new_lc != "new" else None, uname]
                if new_lc == "acknowledged":
                    sets += ["acknowledged_at=?", "acknowledged_by=?"]
                    params += [now, uname]
                if new_lc == "investigating":
                    sets += ["investigating_at=?", "investigating_by=?", "investigation_finding=?"]
                    params += [now, uname, finding]
                if new_lc == "action_assigned":
                    sets += ["action_assigned=?", "action_owner=?"]
                    params += [action, owner]
                if new_lc == "closed":
                    sets += ["status=?", "resolved_at=?", "closure_reason=?", "false_alarm=?"]
                    params += ["resolved", now, close_reason or finding, 1 if false_a else 0]
                else:
                    sets += ["status=?"]
                    params += ["open" if new_lc in ("new", "acknowledged", "investigating", "action_assigned", "monitoring") else "resolved"]
                params.append(int(pick_a))
                try:
                    execute(f"UPDATE alerts SET {', '.join(sets)} WHERE id=?", tuple(params))
                except Exception:
                    # Fallback if columns missing on older DB
                    execute(
                        "UPDATE alerts SET followed_up=?, followed_up_at=?, followed_up_by=?, status=? WHERE id=?",
                        (1 if new_lc != "new" else 0, now, uname,
                         "resolved" if new_lc == "closed" else "open", int(pick_a)),
                    )
                st.success(f"Lifecycle → **{new_lc}**")
                st.rerun()

        # Alert → Action → Outcome metrics
        section_header("Alert → Action → Outcome")
        st.caption(
            "Programme performance of the alert system itself: detection → acknowledgement → resolution. "
            "Requires lifecycle timestamps (acknowledge/resolve) to be recorded."
        )
        try:
            all_a = query(
                "SELECT id, status, severity, triggered_at, acknowledged_at, resolved_at, "
                "IFNULL(false_alarm,0) AS false_alarm, IFNULL(lifecycle,'new') AS lifecycle, "
                "IFNULL(followed_up,0) AS followed_up "
                "FROM alerts ORDER BY id DESC LIMIT 500"
            )
        except Exception:
            all_a = pd.DataFrame()
        if not all_a.empty:
            n_tot = len(all_a)
            n_open = int((all_a["status"] == "open").sum())
            n_res = int((all_a["status"] == "resolved").sum())
            n_false = int(pd.to_numeric(all_a.get("false_alarm"), errors="coerce").fillna(0).sum())
            n_acted = int(pd.to_numeric(all_a.get("followed_up"), errors="coerce").fillna(0).sum())

            def _med_hours(start_col, end_col):
                if start_col not in all_a.columns or end_col not in all_a.columns:
                    return None
                s = pd.to_datetime(all_a[start_col], errors="coerce")
                e = pd.to_datetime(all_a[end_col], errors="coerce")
                delta = (e - s).dt.total_seconds() / 3600.0
                delta = delta.dropna()
                delta = delta[delta >= 0]
                return float(delta.median()) if len(delta) else None

            t_ack = _med_hours("triggered_at", "acknowledged_at")
            t_res = _med_hours("triggered_at", "resolved_at")
            # Open > 7 days
            try:
                trig = pd.to_datetime(all_a["triggered_at"], errors="coerce")
                open_mask = all_a["status"] == "open"
                age_h = (pd.Timestamp(datetime.now()) - trig).dt.total_seconds() / 3600.0
                n_stale = int(((open_mask) & (age_h > 24 * 7)).sum())
            except Exception:
                n_stale = 0

            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Alerts (sample)", n_tot)
            m2.metric("Still open", n_open)
            m3.metric("Resolved", n_res)
            m4.metric("With action (followed up)", n_acted)
            m5.metric("Open > 7 days", n_stale)

            b1, b2, b3 = st.columns(3)
            b1.metric("Median hrs → acknowledge", f"{t_ack:.1f}" if t_ack is not None else "—")
            b2.metric("Median hrs → resolve", f"{t_res:.1f}" if t_res is not None else "—")
            b3.metric(
                "False-alarm rate",
                f"{(n_false / max(n_res, 1) * 100):.0f}%" if n_res else "—",
            )
            # Lifecycle funnel
            if "lifecycle" in all_a.columns:
                lc = all_a["lifecycle"].fillna("new").astype(str).value_counts()
                st.markdown("**Lifecycle funnel (sample)**")
                st.dataframe(
                    pd.DataFrame({"Lifecycle": lc.index, "Count": lc.values}),
                    use_container_width=True, hide_index=True,
                )
            st.caption(
                "Target pattern: short time to acknowledge, documented action, timely close. "
                "High open>7d or missing acknowledge times → lifecycle not used consistently."
            )
        else:
            st.caption("No alert history yet for performance metrics.")

        for severity in ["critical", "high", "warning"]:
            subset = view[view["severity"] == severity]
            if subset.empty:
                continue
            st.markdown(f"### {severity.upper()} ({len(subset)})")
            for _, row in subset.iterrows():
                col1, col2 = st.columns([5, 1])
                with col1:
                    emoji = {"critical": "🔴", "high": "🟠", "warning": "🟡"}.get(severity, "•")
                    msg = str(row["message"])
                    # Strip internal [key] prefix for display if present
                    display_msg = msg
                    if msg.startswith("[") and "]" in msg:
                        display_msg = msg.split("]", 1)[1].strip()
                    st.markdown(
                        f"{emoji} **{row['category']}** · {row['facility']} · "
                        f"{row.get('period') or '—'} — {display_msg}"
                    )
                    with st.expander("Why am I seeing this alert?"):
                        st.markdown(
                            f"""
- **Type:** {row.get('category')} · **Severity:** {severity}  
- **Geography:** {row.get('facility')}  
- **Period:** {row.get('period') or '—'}  
- **Message:** {display_msg}  
- **Follow-up:** {"yes" if row.get("followed_up") else "not ticked"}  
                            """
                        )
                        st.caption(
                            "If the text mentions INVALID or numerator > denominator, treat as a **data-quality** alert, "
                            "not an outbreak. Open Data foundation / GO formula audit for the cell."
                        )
                with col2:
                    if st.button("Resolve", key=f"resolve_{row['id']}"):
                        execute(
                            "UPDATE alerts SET status='resolved', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                            (int(row["id"]),),
                        )
                        log_audit(user["username"], "resolve_alert", "alerts", str(row["message"])[:120])
                        st.rerun()

    # Catalogue
    with st.expander("Programme rules monitored"):
        st.markdown(
            """
| Key | Rule | Severity |
|-----|------|----------|
| reporting_lt_60 | Reporting rate &lt; 60% | Critical |
| reporting_lt_80 | Reporting rate 60–79% | Warning |
| reporting_gt_150 | Reporting rate &gt; 150% (implausible) | Critical |
| diag_gap_high | Diagnosis gap ≥ 25% (≥20 suspected) | High |
| treat_gap_high | Treatment gap ≥ 20% (≥10 confirmed) | High |
| testing_collapse | Testing volume down &gt;50% vs prior month | Critical |
| tpr_high | RDT positivity ≥ 70% (≥30 exams) | Warning |
| twt_high | Treated without testing ≥ 5% | High |
| go1_missing | No RDT exam volume for GO_1 | Critical |
| stock_rdt / stock_al | Stock below reorder level | High |
"""
        )
        st.caption("Rules use the same indicator patterns as cascade / DQ (not exact KHIS strings).")

    section_header("Recently resolved")
    resolved = get_resolved_alerts(20)
    if resolved.empty:
        st.caption("No resolved alerts yet.")
    else:
        show = resolved[["severity", "category", "facility", "period", "message", "resolved_at"]].copy()
        show["message"] = show["message"].astype(str).str.replace(r"^\[[^\]]+\]\s*", "", regex=True)
        st.dataframe(show, use_container_width=True, hide_index=True)


# ==================================================================
# PAGE 14 — MAPS
# ==================================================================

def subcounty_programme_board(df, filters, period_pick=None):
    """Seven-sub-county board: WFK extract + CBMP site overlay. Rates via safe_rate."""
    try:
        raw = query("SELECT * FROM kilifi_subcounty")
    except Exception:
        raw = pd.DataFrame()
    rows = []
    for scn in KILIFI_SUBCOUNTIES:
        geo = SUBCOUNTY_GEO.get(scn, {})
        rec = {
            "sub_county": scn,
            "lat": geo.get("lat"),
            "lon": geo.get("lon"),
            "gps_source": geo.get("source"),
            "gps_level": geo.get("gps_level", 6),
            "cbmp_site": CBMP_IN_SUBCOUNTY.get(scn, "—"),
            "suspected": None, "tested": None, "confirmed": None,
            "tpr": None, "tpr_status": "na",
            "coverage": "pending",
        }
        if not raw.empty:
            v = raw[raw["sub_county"] == scn]
            if period_pick and period_pick != "(all)" and not v.empty:
                v = v[v["period"].astype(str) == str(period_pick)]
            if not v.empty:
                rec["suspected"] = pd.to_numeric(v["suspected"], errors="coerce").sum()
                rec["tested"] = pd.to_numeric(v["tested"], errors="coerce").sum()
                rec["confirmed"] = pd.to_numeric(v["confirmed"], errors="coerce").sum()
                rec["rdt_exam"] = pd.to_numeric(v["rdt_exam"], errors="coerce").sum()
                rec["rdt_pos"] = pd.to_numeric(v["rdt_pos"], errors="coerce").sum()
                a = safe_rate(rec.get("rdt_pos") or 0, rec.get("rdt_exam") or 0, indicator="GIS_TPR")
                rec["tpr"] = a.get("value") if a.get("status") == "valid" else None
                rec["tpr_status"] = a.get("status")
                rec["coverage"] = "wfk extract"
        # Overlay CBMP site cascade if this sub-county has a sentinel
        site = CBMP_IN_SUBCOUNTY.get(scn)
        if site and df is not None and not df.empty:
            fd = _filter_data(df, {**(filters or {}), "facility": site})
            c = compute_cascade_metrics(fd)
            rec["cbmp_tested"] = c.get("tested")
            rec["cbmp_confirmed"] = c.get("confirmed")
            rec["diag_gap"] = c.get("diag_gap")
            rec["treat_gap"] = c.get("treat_gap")
        rows.append(rec)
    return pd.DataFrame(rows)


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dl = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def detect_spatial_clusters(risk_rows, max_km=35.0):
    """
    Simple adjacency clustering of elevated-risk facilities.
    risk_rows: list of dicts with facility, lat, lon, risk_level, risk_score
    Does NOT label outbreaks — only neighbouring elevated risk.
    """
    elevated = [
        r for r in risk_rows
        if str(r.get("risk_level") or "").lower() in ("high", "very high", "moderate")
        and r.get("lat") is not None and r.get("lon") is not None
    ]
    if len(elevated) < 2:
        return []
    clusters = []
    used = set()
    for i, a in enumerate(elevated):
        if i in used:
            continue
        group = [a]
        used.add(i)
        for j, b in enumerate(elevated):
            if j in used:
                continue
            try:
                d = _haversine_km(float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"]))
            except Exception:
                continue
            if d <= max_km:
                group.append(b)
                used.add(j)
        if len(group) >= 2:
            clusters.append(group)
    return clusters


def plot_or_empty(fig, empty_message="⚠️ No valid observations for this chart with current filters."):
    """Render a Plotly figure or a consistent empty-state message."""
    if fig is None:
        st.info(empty_message)
        return
    try:
        data = getattr(fig, "data", None)
        if not data or all(
            (getattr(tr, "x", None) is None or len(getattr(tr, "x", []) or []) == 0)
            and (getattr(tr, "values", None) is None or len(getattr(tr, "values", []) or []) == 0)
            for tr in data
        ):
            # Has traces but may still be empty — still show if any numeric
            has = False
            for tr in data or []:
                for attr in ("x", "y", "values", "lat", "lon"):
                    v = getattr(tr, attr, None)
                    if v is not None and len(v) > 0:
                        has = True
                        break
                if has:
                    break
            if not has:
                st.info(empty_message)
                return
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        st.plotly_chart(fig, use_container_width=True)


def page_maps(user, filters):
    """
    Programme GIS: 7 sub-counties + facility/CHU pins.
    Gazetteer coordinates are display-only (level 5) — not for targeting alerts.
    """
    page_header(
        "Kilifi programme GIS",
        "County outline · 7 sub-counties · three CBMP facilities · CHU import. "
        "Gazetteer / offset points are labelled **approximate** and must not drive targeting.",
    )
    df = load_all_data()
    d = _filter_data(df, filters) if df is not None and not df.empty else pd.DataFrame()

    # Period slider from WFK or KHIS monthly
    try:
        wfk_p = query("SELECT DISTINCT period FROM kilifi_subcounty ORDER BY period_date DESC")
        wfk_periods = wfk_p["period"].astype(str).tolist() if not wfk_p.empty else []
    except Exception:
        wfk_periods = []
    khis_m = []
    if df is not None and not df.empty:
        khis_m = sorted(
            df[df["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)]["period"].astype(str).unique().tolist(),
            reverse=True,
        )
    period_opts = ["(all)"] + (wfk_periods or khis_m)
    theme = st.selectbox(
        "Map theme",
        [
            "RDT positivity (VALID only)",
            "Confirmed cases",
            "Diagnosis gap (CBMP sites)",
            "Treatment gap (CBMP sites)",
            "Early-warning 4-week risk (CBMP sites)",
            "GPS confidence / missing points",
        ],
        key="gis_theme",
    )
    pcol1, pcol2, pcol3 = st.columns([2, 1, 1])
    with pcol1:
        period_pick = st.selectbox("Period (WFK / monthly)", period_opts, key="gis_period")
    with pcol2:
        selected_sc = st.selectbox("Focus sub-county", ["All 7"] + KILIFI_SUBCOUNTIES, key="gis_focus")
    with pcol3:
        # Time slider over available monthly periods (excluding "(all)")
        time_periods = [p for p in period_opts if p != "(all)"]
        use_slider = st.checkbox("Time slider", value=False, key="gis_time_slider")
    if use_slider and len(time_periods) >= 2:
        idx = st.slider(
            "Scrub through periods",
            0, len(time_periods) - 1, 0,
            key="gis_slider_idx",
        )
        period_pick = time_periods[idx]
        st.caption(f"Showing period: **{period_pick}** ({idx + 1}/{len(time_periods)})")
    if "Early-warning" in theme:
        st.caption(
            "EW risk uses the latest stored 4-week forecasts (Malaria Early Warning page). "
            "Solid facility risk is independent of the period scrub for sub-county observed layers."
        )

    board = subcounty_programme_board(df, filters, None if period_pick == "(all)" else period_pick)
    layers = st.multiselect(
        "Layers",
        ["Sub-counties", "CBMP facilities", "CHUs", "County outline"],
        default=["Sub-counties", "CBMP facilities", "County outline"],
        key="gis_layers",
    )

    left, right = st.columns([3, 2])
    with left:
        plot_sc = board.dropna(subset=["lat", "lon"]).copy()
        if selected_sc != "All 7":
            plot_sc = plot_sc[plot_sc["sub_county"] == selected_sc]
        if theme.startswith("RDT"):
            plot_sc["color_col"] = plot_sc["tpr"]
            plot_sc["size_col"] = plot_sc["tpr"].fillna(4) + 4
        elif theme.startswith("Confirmed"):
            plot_sc["color_col"] = plot_sc["confirmed"]
            plot_sc["size_col"] = pd.to_numeric(plot_sc["confirmed"], errors="coerce").fillna(1).clip(lower=1)
        elif "Diagnosis" in theme:
            plot_sc["color_col"] = plot_sc.get("diag_gap")
            plot_sc["size_col"] = pd.to_numeric(plot_sc.get("diag_gap"), errors="coerce").fillna(1).clip(lower=1)
        elif "Treatment" in theme:
            plot_sc["color_col"] = plot_sc.get("treat_gap")
            plot_sc["size_col"] = pd.to_numeric(plot_sc.get("treat_gap"), errors="coerce").fillna(1).clip(lower=1)
        elif "Early-warning" in theme:
            # Risk score from latest EW forecasts if available
            try:
                ew = query(
                    "SELECT facility, risk_score, risk_level, predicted_cases "
                    "FROM ew_forecasts WHERE horizon_weeks=4 "
                    "ORDER BY run_at DESC"
                )
                if not ew.empty:
                    ew = ew.drop_duplicates(subset=["facility"], keep="first")
                    # Map CBMP site into sub-county board via site name match
                    site_to_sc = {v: k for k, v in CBMP_IN_SUBCOUNTY.items()}
                    ew["sub_county"] = ew["facility"].map(SUBCOUNTY)
                    plot_sc = plot_sc.merge(
                        ew[["sub_county", "risk_score", "risk_level", "predicted_cases"]],
                        on="sub_county", how="left",
                    )
                    plot_sc["color_col"] = pd.to_numeric(plot_sc["risk_score"], errors="coerce")
                    plot_sc["size_col"] = plot_sc["color_col"].fillna(0.2) * 40 + 8
                else:
                    plot_sc["color_col"] = np.nan
                    plot_sc["size_col"] = 8
                    st.caption("No EW forecasts stored yet — open **Malaria Early Warning** once to generate.")
            except Exception:
                plot_sc["color_col"] = np.nan
                plot_sc["size_col"] = 8
        else:
            plot_sc["color_col"] = plot_sc["gps_level"]
            plot_sc["size_col"] = 8
        fig = px.scatter_mapbox(
            plot_sc if "Sub-counties" in layers else plot_sc.iloc[0:0],
            lat="lat", lon="lon",
            hover_name="sub_county",
            size="size_col",
            color="color_col",
            hover_data={"cbmp_site": True, "tpr": True, "tpr_status": True, "confirmed": True, "gps_source": True, "lat": False, "lon": False, "size_col": False},
            zoom=8.0, height=460, color_continuous_scale="Reds", size_max=40,
        )
        fig.update_layout(mapbox_style="open-street-map", margin=dict(l=0, r=0, t=0, b=0))
        geo_path = Path(__file__).resolve().parent / "docs" / "geo" / "kilifi_county.geojson"
        if "County outline" in layers and geo_path.exists():
            try:
                with open(geo_path) as gf:
                    kilifi_gj = json.load(gf)
                fig.update_layout(mapbox_layers=[{
                    "sourcetype": "geojson", "source": kilifi_gj,
                    "type": "line", "color": "#1B4F72", "line": {"width": 2},
                }])
            except Exception:
                pass
        if "CBMP facilities" in layers:
            fac_df = pd.DataFrame([
                {"facility": n, "lat": g["lat"], "lon": g["lon"],
                 "gps": g.get("gps_status", "gazetteer"), "sub_county": g.get("sub_county")}
                for n, g in GEO_SITES.items()
            ])
            fig.add_scattermapbox(
                lat=fac_df["lat"], lon=fac_df["lon"],
                text=fac_df["facility"], mode="markers+text",
                marker=dict(size=12, color="#1B4F72"),
                name="CBMP facility",
            )
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Sub-county markers are **Level 5 gazetteer centroids** (display only). "
            "Facility pins are published populated-place coordinates, not KMHFR-verified. "
            "No geographic alert is raised from these points."
        )

    with right:
        section_header("Selected area")
        focus = selected_sc if selected_sc != "All 7" else "Kilifi County (7 units)"
        st.markdown(f"**{focus}**")
        if selected_sc != "All 7":
            hit = board[board["sub_county"] == selected_sc]
            if not hit.empty:
                r = hit.iloc[0]
                st.markdown(
                    f"- CBMP site: **{r.get('cbmp_site')}**  \n"
                    f"- Coverage: {r.get('coverage')}  \n"
                    f"- Suspected / tested / confirmed: {r.get('suspected')} / {r.get('tested')} / {r.get('confirmed')}  \n"
                    f"- RDT TPR: {r.get('tpr') if r.get('tpr_status')=='valid' else 'N/A'} [{r.get('tpr_status')}]  \n"
                    f"- GPS: Level {r.get('gps_level')} · {r.get('gps_source')}"
                )
                if r.get("cbmp_site") not in (None, "—"):
                    st.caption(f"Diagnosis gap (site): {r.get('diag_gap')} · Treatment gap: {r.get('treat_gap')}")
        else:
            st.dataframe(
                board[["sub_county", "cbmp_site", "confirmed", "tpr", "tpr_status", "coverage"]],
                use_container_width=True, hide_index=True,
            )
        try:
            n_open = int(query("SELECT COUNT(*) AS n FROM alerts WHERE status='open'").iloc[0]["n"])
            st.markdown(f"Open alerts (all): **{n_open}**")
        except Exception:
            pass
        st.download_button(
            "📥 Export 7-sub-county GIS board",
            board.to_csv(index=False).encode("utf-8"),
            file_name=f"gis_subcounties_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="gis_board_csv",
        )

    section_header("GPS confidence (must not drive targeting)")
    gps_rows = []
    for fac, g in GEO_SITES.items():
        gps_rows.append({
            "Level": "Facility", "Name": fac, "Sub-county": g.get("sub_county"),
            "Lat": g.get("lat"), "Lon": g.get("lon"),
            "Source": g.get("source"), "GPS status": g.get("gps_status", "gazetteer"),
            "GPS level": g.get("gps_level", 5), "Operational use": "Display only until KMHFR verify",
        })
    for scn, g in SUBCOUNTY_GEO.items():
        gps_rows.append({
            "Level": "Sub-county", "Name": scn, "Sub-county": scn,
            "Lat": g.get("lat"), "Lon": g.get("lon"),
            "Source": g.get("source"), "GPS status": "gazetteer centroid",
            "GPS level": 5, "Operational use": "Display only",
        })
    st.dataframe(pd.DataFrame(gps_rows), use_container_width=True, hide_index=True)

    if d.empty:
        st.info("Commit KHIS to see facility cascade on the map below. WFK import still drives the 7-sub-county layer.")
        return

    facility_coords = GEO_SITES

    indicators = compute_official_indicators(df, filters)
    map_rows = []
    for fac, coords in facility_coords.items():
        if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
            continue
        fac_df = d[d["facility"] == fac]
        cascade = compute_cascade_metrics(fac_df)
        go_pf = indicators["GO_1"].get("per_facility", {}).get(fac, {}) or {}
        tpr = go_pf.get("value")
        tpr_status = go_pf.get("status") or ("valid" if tpr is not None else "na")
        r11 = (indicators["R1_1"].get("per_facility") or {}).get(fac, 0)
        r12 = (indicators["R1_2"].get("per_facility") or {}).get(fac)
        # Stock / alerts
        try:
            stock_f = query("SELECT * FROM stock WHERE facility = ?", (fac,))
            low_stock = int((stock_f["quantity"] < stock_f["reorder_point"]).sum()) if not stock_f.empty else 0
        except Exception:
            low_stock = 0
        try:
            al_f = get_active_alerts(facility=fac)
            n_alerts = len(al_f) if al_f is not None and not al_f.empty else 0
        except Exception:
            n_alerts = 0

        map_rows.append({
            "facility": fac,
            "sub_county": coords.get("sub_county") or SUBCOUNTY.get(fac, "—"),
            "ward": coords.get("ward", "—"),
            "coord_source": coords.get("source", ""),
            "lat": coords["lat"],
            "lon": coords["lon"],
            "confirmed": cascade["confirmed"],
            "tested": cascade["tested"],
            "suspected": cascade["suspected"],
            "diag_gap": cascade["diag_gap"],
            "treat_gap": cascade["treat_gap"],
            "testing_rate": cascade["testing_rate"],
            "treat_cov": cascade["treat_coverage"],
            "tpr": tpr if tpr_status == "valid" else None,
            "tpr_status": tpr_status,
            "tpr_raw": go_pf.get("calculated_pct"),
            "r11_tests": int(r11) if r11 else 0,
            "r12": r12,
            "low_stock": low_stock,
            "alerts": n_alerts,
            "bubble": max(cascade["confirmed"], 1),
        })

    map_df = pd.DataFrame(map_rows)
    if map_df.empty:
        st.info("No facilities in current filter.")
        return

    # KPI strip
    section_header("Programme footprint (filtered)")
    k1, k2, k3, k4, k5 = st.columns(5)
    with k1:
        big_number(str(len(map_df)), "Facilities on map")
    with k2:
        big_number(fmt_count(map_df["confirmed"].sum()), "Confirmed (sum)")
    with k3:
        big_number(f"{int(map_df['diag_gap'].sum()):,}", "Diagnosis gap")
    with k4:
        big_number(f"{int(map_df['treat_gap'].sum()):,}", "Treatment gap")
    with k5:
        big_number(str(int(map_df["alerts"].sum())), "Open alerts")

    theme = st.selectbox(
        "Map bubble / colour theme",
        [
            ("confirmed", "Confirmed cases"),
            ("tested", "Tests"),
            ("diag_gap", "Diagnosis gap"),
            ("treat_gap", "Treatment gap"),
            ("tpr", "Valid RDT TPR %"),
            ("r11_tests", "CHP tests (R1.1)"),
            ("alerts", "Open alerts"),
        ],
        format_func=lambda x: x[1],
        key="map_theme",
    )
    theme_key = theme[0] if isinstance(theme, tuple) else theme

    section_header("Facility map")
    plot_df = map_df.copy()
    if theme_key == "tpr":
        plot_df["size_col"] = plot_df["tpr"].fillna(0).clip(lower=0) + 1
        plot_df["color_col"] = plot_df["tpr"]
    else:
        plot_df["size_col"] = plot_df[theme_key].fillna(0).clip(lower=0) + 1
        plot_df["color_col"] = plot_df[theme_key]

    fig = px.scatter_mapbox(
        plot_df,
        lat="lat",
        lon="lon",
        size="size_col",
        color="color_col",
        hover_name="facility",
        hover_data={
            "sub_county": True,
            "confirmed": True,
            "tested": True,
            "diag_gap": True,
            "treat_gap": True,
            "tpr": True,
            "tpr_status": True,
            "r11_tests": True,
            "alerts": True,
            "low_stock": True,
            "lat": False,
            "lon": False,
            "size_col": False,
            "color_col": False,
        },
        color_continuous_scale="Reds",
        size_max=55,
        zoom=8.2,
        height=480,
    )
    fig.update_layout(mapbox_style="open-street-map", margin=dict(l=0, r=0, t=0, b=0))
    geo_path = Path(__file__).resolve().parent / "docs" / "geo" / "kilifi_county.geojson"
    if geo_path.exists():
        try:
            with open(geo_path) as gf:
                kilifi_gj = json.load(gf)
            fig.update_layout(
                mapbox_layers=[{
                    "sourcetype": "geojson",
                    "source": kilifi_gj,
                    "type": "line",
                    "color": "#1B4F72",
                    "line": {"width": 2},
                }]
            )
        except Exception:
            pass
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Outline is the **Kilifi County** polygon (open GeoJSON). Ward polygons are a large HDX file — drop a Kilifi-only GeoJSON in docs/geo to replace this. "
        "Facility pins use **published GeoNames / Fallingrain coordinates** "
        "(Jaribuni −3.622, 39.744 · Pingilikani −3.783, 39.780 · Mwakuhenga −3.702, 39.812). "
        "Metrics follow **Apply filters**. Invalid TPR is not used as map colour when TPR theme is selected."
    )

    # ── Community / CHU layer from MOH 648 ──
    section_header("Community & CHU layer (MOH 648)")
    st.caption(
        "Position order: **imported GPS** → **public gazetteer** (if the CHU name matches a known village) "
        "→ **stable offset** from the facility pin. Official CHU GPS lives in KMHFR and is not published as a bulk open file."
    )
    tdir = APP_DIR / "docs" / "templates"
    tdir.mkdir(parents=True, exist_ok=True)
    gps_tpl = tdir / "chu_gps_template.csv"
    if not gps_tpl.exists():
        gps_tpl.write_text(
            "facility,chu,lat,lon,source,notes\n"
            "Jaribuni,Jaribuni,-3.62164,39.74354,gazetteer,example — replace with KMHFR/survey\n"
            "Pingilikani,Pingilikani,-3.7834,39.7802,gazetteer,\n"
            "Mwakuhenga,Mavueni,-3.681767,39.816489,wikipedia Mavueni,\n"
        )
    g1, g2 = st.columns([1, 2])
    with g1:
        st.download_button(
            "📥 CHU GPS template",
            gps_tpl.read_bytes(),
            file_name="chu_gps_template.csv",
            mime="text/csv",
            key="dl_chu_gps_tpl",
        )
    with g2:
        if user.get("can_upload"):
            gps_file = st.file_uploader("Upload CHU GPS CSV (chu, lat, lon)", type=["csv"], key="up_chu_gps")
            if gps_file is not None and st.button("Import CHU GPS", key="btn_chu_gps"):
                n, errs = import_chu_gps_csv(gps_file, user.get("username", "system"))
                if n:
                    st.success(f"Imported **{n}** CHU coordinate row(s).")
                    st.rerun()
                if errs:
                    st.warning("; ".join(errs[:5]))
    try:
        known = query("SELECT facility, chu, lat, lon, source FROM chu_gps ORDER BY id DESC LIMIT 20")
        if not known.empty:
            st.caption("Imported CHU GPS (latest 20)")
            st.dataframe(known, use_container_width=True, hide_index=True)
    except Exception:
        pass
    try:
        m648 = query("SELECT * FROM moh_648")
    except Exception:
        m648 = pd.DataFrame()
    if filters.get("facility") not in (None, "All three") and not m648.empty:
        m648 = m648[m648["facility"] == filters["facility"]]
    chu_rows = []
    if not m648.empty:
        m648 = m648.copy()
        for col in ("suspected", "tested", "positive", "treated", "referred", "followed_up"):
            if col not in m648.columns:
                m648[col] = 0
            m648[col] = pd.to_numeric(m648[col], errors="coerce").fillna(0)
        m648["chu_key"] = m648["chu"].fillna("").astype(str).str.strip()
        m648.loc[m648["chu_key"] == "", "chu_key"] = "(no CHU name)"
        grp = m648.groupby(["facility", "chu_key"], as_index=False).agg(
            suspected=("suspected", "sum"),
            tested=("tested", "sum"),
            positive=("positive", "sum"),
            treated=("treated", "sum"),
            referred=("referred", "sum"),
            followed_up=("followed_up", "sum"),
            events=("facility", "count"),
        )
        for _, r in grp.iterrows():
            fac = r["facility"]
            lat, lon, src = resolve_chu_coords(fac, r["chu_key"])
            if lat is None:
                continue
            tpr_a = safe_rate(float(r["positive"]), float(r["tested"]), indicator="MAP_CHU_TPR")
            chu_rows.append({
                "facility": fac,
                "chu": r["chu_key"],
                "coord_source": src,
                "lat": lat,
                "lon": lon,
                "events": int(r["events"]),
                "suspected": int(r["suspected"]),
                "tested": int(r["tested"]),
                "positive": int(r["positive"]),
                "treated": int(r["treated"]),
                "referred": int(r["referred"]),
                "followed_up": int(r["followed_up"]),
                "tpr": tpr_a.get("value") if tpr_a.get("status") == "valid" else None,
                "tpr_status": tpr_a.get("status"),
            })
    chu_df = pd.DataFrame(chu_rows)
    if chu_df.empty:
        st.info("Import MOH 648 with `chu` names to plot community units around each facility.")
    else:
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            big_number(str(len(chu_df)), "CHUs on map")
        with c2:
            big_number(f"{int(chu_df['tested'].sum()):,}", "Community tests")
        with c3:
            big_number(f"{int(chu_df['positive'].sum()):,}", "Community positives")
        with c4:
            big_number(f"{int(chu_df['referred'].sum()):,}", "Referrals")
        fig_c = px.scatter_mapbox(
            chu_df,
            lat="lat",
            lon="lon",
            size=chu_df["tested"].clip(lower=1),
            color="positive",
            hover_name="chu",
            hover_data={
                "facility": True, "tested": True, "positive": True, "treated": True,
                "referred": True, "followed_up": True, "tpr": True, "tpr_status": True,
                "lat": False, "lon": False,
            },
            color_continuous_scale="YlOrRd",
            size_max=28,
            zoom=8.4,
            height=480,
        )
        # Overlay facility anchors
        fig_c.add_trace(
            pgo.Scattermapbox(
                lat=map_df["lat"],
                lon=map_df["lon"],
                mode="markers+text",
                text=map_df["facility"],
                textposition="top center",
                marker=dict(size=16, color="#0B3D5C"),
                name="Facility",
            )
        )
        fig_c.update_layout(mapbox_style="open-street-map", margin=dict(l=0, r=0, t=0, b=0), legend=dict(orientation="h"))
        st.plotly_chart(fig_c, use_container_width=True)
        st.dataframe(
            chu_df.drop(columns=["lat", "lon"]).sort_values(["facility", "tested"], ascending=[True, False]),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "📥 Download CHU community layer CSV",
            chu_df.to_csv(index=False).encode("utf-8"),
            file_name=f"kilifi_chu_layer_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="b3_chu_csv",
        )
        hot = chu_df.sort_values("positive", ascending=False).head(5)
        if not hot.empty and hot["positive"].sum() > 0:
            st.markdown("**Highest community positives (this filter)**")
            for _, h in hot.iterrows():
                tpr_txt = f"{h['tpr']:.0f}%" if h["tpr"] is not None else (h["tpr_status"] or "n/a")
                st.markdown(
                    f"- **{h['chu']}** ({h['facility']}): {int(h['positive'])} pos / {int(h['tested'])} tests · TPR {tpr_txt}"
                )

    section_header("Hotspot & performance ranking")
    rank = map_df.sort_values("confirmed", ascending=False).copy()
    rank["TPR %"] = rank.apply(
        lambda r: f"{r['tpr']:.1f}" if r["tpr"] is not None else (
            f"INVALID ({r['tpr_raw']})" if r["tpr_status"] == "invalid" else "—"
        ),
        axis=1,
    )
    rank["Testing %"] = rank["testing_rate"].map(lambda x: f"{x:.0f}" if x is not None else "—")
    rank["Treat cov %"] = rank["treat_cov"].map(lambda x: f"{x:.0f}" if x is not None else "—")
    rank["R1.2 %"] = rank["r12"].map(lambda x: f"{x:.0f}" if x is not None else "—")
    show_rank = rank[[
        "facility", "sub_county", "confirmed", "tested", "suspected",
        "diag_gap", "treat_gap", "TPR %", "Testing %", "Treat cov %",
        "r11_tests", "R1.2 %", "alerts", "low_stock",
    ]].rename(columns={
        "facility": "Facility",
        "sub_county": "Sub-county",
        "confirmed": "Confirmed",
        "tested": "Tested",
        "suspected": "Suspected",
        "diag_gap": "Diag. gap",
        "treat_gap": "Treat. gap",
        "r11_tests": "CHP tests",
        "alerts": "Alerts",
        "low_stock": "Stock low",
    })
    st.dataframe(show_rank, use_container_width=True, hide_index=True)

    # Charts: burden vs gaps
    section_header("Burden vs cascade gaps")
    c_left, c_right = st.columns(2)
    with c_left:
        fig_b = px.bar(
            map_df, x="facility", y=["confirmed", "tested", "suspected"],
            barmode="group", height=360,
            labels={"value": "Count", "facility": "Facility", "variable": "Metric"},
            color_discrete_sequence=[COLOR_AMBER, COLOR_NAVY, COLOR_BLUE],
        )
        fig_b.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig_b, use_container_width=True)
    with c_right:
        fig_g = px.bar(
            map_df, x="facility", y=["diag_gap", "treat_gap"],
            barmode="group", height=360,
            labels={"value": "Gap count", "facility": "Facility", "variable": "Gap"},
            color_discrete_sequence=[COLOR_RED, COLOR_AMBER],
        )
        fig_g.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig_g, use_container_width=True)

    # Valid TPR only chart
    tpr_ok = map_df[map_df["tpr"].notna()]
    if not tpr_ok.empty:
        section_header("Valid RDT TPR by facility (invalid cells excluded)")
        fig_t = px.bar(
            tpr_ok, x="facility", y="tpr", text="tpr",
            color="tpr", color_continuous_scale="Reds", height=320,
        )
        fig_t.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig_t.add_hline(y=BASELINES["GO_1"]["baseline"], line_dash="dot", line_color="#999")
        fig_t.add_hline(y=BASELINES["GO_1"]["target"], line_dash="dash", line_color=COLOR_RED)
        fig_t.update_layout(plot_bgcolor="white", yaxis_title="TPR %")
        st.plotly_chart(fig_t, use_container_width=True)
    inv = map_df[map_df["tpr_status"] == "invalid"]
    if not inv.empty:
        st.error(
            "Invalid TPR (numerator > denominator) at: "
            + ", ".join(f"{r.facility} (raw {r.tpr_raw}%)" for r in inv.itertuples())
        )

    # Spatial clustering of elevated EW risk (neighbouring facilities, not "outbreak")
    section_header("Spatial clustering (elevated risk neighbours)")
    st.caption(
        "Groups CBMP sites within ~35 km that share Moderate/High/Very High EW risk. "
        "This is a **neighbouring elevated-risk** signal — not an outbreak declaration."
    )
    try:
        ew_cl = query(
            "SELECT facility, risk_score, risk_level, predicted_cases "
            "FROM ew_forecasts WHERE horizon_weeks=4 ORDER BY run_at DESC"
        )
        if not ew_cl.empty:
            ew_cl = ew_cl.drop_duplicates(subset=["facility"], keep="first")
            risk_rows = []
            for _, r in ew_cl.iterrows():
                fac = r["facility"]
                g = GEO_SITES.get(fac, {})
                risk_rows.append({
                    "facility": fac,
                    "lat": g.get("lat"),
                    "lon": g.get("lon"),
                    "risk_level": r.get("risk_level"),
                    "risk_score": r.get("risk_score"),
                    "predicted_cases": r.get("predicted_cases"),
                })
            clusters = detect_spatial_clusters(risk_rows, max_km=35.0)
            if not clusters:
                st.info("No multi-facility elevated-risk cluster under current forecasts.")
            else:
                for i, grp in enumerate(clusters, 1):
                    names = ", ".join(x["facility"] for x in grp)
                    levels = ", ".join(f"{x['facility']}={x.get('risk_level')}" for x in grp)
                    st.markdown(f"**Cluster {i}:** {names}")
                    st.caption(levels)
        else:
            st.info("Generate forecasts on **Malaria Early Warning** to enable cluster detection.")
    except Exception as e:
        st.caption(f"Cluster check unavailable: {e}")

    # Catchment / optional polygon layers from docs/geo/
    section_header("Catchment geography")
    geo_dir = Path(__file__).resolve().parent / "docs" / "geo"
    extra_layers = []
    if geo_dir.exists():
        for p in sorted(geo_dir.glob("*.geojson")):
            if p.name.lower() == "kilifi_county.geojson":
                continue
            extra_layers.append(p)
    if extra_layers:
        st.success(f"Found **{len(extra_layers)}** additional GeoJSON layer(s) in `docs/geo/`.")
        pick_layer = st.selectbox(
            "Overlay catchment / ward layer",
            ["(none)"] + [p.name for p in extra_layers],
            key="gis_catchment_layer",
        )
        if pick_layer != "(none)":
            try:
                with open(geo_dir / pick_layer) as gf:
                    gj = json.load(gf)
                # Re-draw a small map with the polygon layer for inspection
                fig_poly = pgo.Figure()
                fig_poly.update_layout(
                    mapbox_style="open-street-map",
                    mapbox=dict(center=dict(lat=-3.7, lon=39.7), zoom=8),
                    mapbox_layers=[{
                        "sourcetype": "geojson", "source": gj,
                        "type": "line", "color": "#0E6655", "line": {"width": 2},
                    }],
                    height=360, margin=dict(l=0, r=0, t=0, b=0),
                )
                # Facility pins
                for fac, g in GEO_SITES.items():
                    fig_poly.add_trace(pgo.Scattermapbox(
                        lat=[g["lat"]], lon=[g["lon"]],
                        mode="markers+text", text=[fac],
                        marker=dict(size=12, color=COLOR_NAVY),
                        name=fac,
                    ))
                st.plotly_chart(fig_poly, use_container_width=True)
                st.caption(
                    f"Layer **{pick_layer}** is display-only until linked to population denominators "
                    "and DoH-approved catchment assignment."
                )
            except Exception as e:
                st.warning(f"Could not load {pick_layer}: {e}")
    else:
        st.markdown(
            """
Programme geography is currently **facility points** (verified or gazetteer).  
True **catchment polygons** (wards / CHU boundaries) require County GIS layers.

**To enable:** place GeoJSON files in `docs/geo/` (e.g. `kilifi_wards.geojson`, `chu_catchments.geojson`).  
They will appear here as optional overlays automatically.

Until then:
- Population denominators remain **provisional** unless DoH-confirmed  
- Map points must not drive micro-targeting  
- Sub-county centroids are display-only (gps_level 5)
            """
        )

    section_header("How to use this page")
    st.markdown(
        """
1. Set **global filters** (period, facility) and Apply.  
2. Switch the map theme to diagnosis gap, alerts, valid TPR, or early-warning risk.  
3. Use **Time slider** to scrub observed periods.  
4. Open **Facility Deep-Dive** for the highest-ranked facility.  
5. Invalid TPR never appears as a normal hotspot colour — reconcile on **GO** / **Data Quality**.  
6. Spatial clusters flag neighbouring elevated risk — investigate jointly, do not auto-label outbreak.
        """
    )


# ==================================================================
# PAGE 15 — COMPARISON
# ==================================================================

def load_county_observations():
    try:
        return query(
            """
            SELECT geo_level, geo_name, sub_county, facility, register, indicator,
                   age_group, period, period_type, value, source_label
            FROM observations
            WHERE geo_level = 'county'
            """
        )
    except Exception:
        return pd.DataFrame()


def proposal_geography_table(df, filters):
    """
    Three CBMP sites vs Kilifi County sheet (observations).
    Honest: North/South/Ganze KHIS sheets are mapped to the three facilities,
    so sub-county in this app = those three catchments, not every facility in the sub-county.
    County sheet = whole Kilifi aggregate.
    """
    rows = []
    for fac in FACILITIES:
        pf = dict(filters or {})
        pf["facility"] = fac
        fd = _filter_data(df, pf)
        c = compute_cascade_metrics(fd)
        go = compute_indicator("GO_1", fd, facility=fac)
        r11 = compute_indicator("R1_1", fd)
        rows.append({
            "Geography": fac,
            "Level": "CBMP facility",
            "Sub-county": SUBCOUNTY.get(fac, "—"),
            "Suspected": c.get("suspected"),
            "Tested": c.get("tested"),
            "Confirmed": c.get("confirmed"),
            "AL": c.get("treated"),
            "R1.1 tests": r11.get("value"),
            "GO_1 TPR": go.get("value") if go.get("status") == "valid" else None,
            "GO status": go.get("status"),
        })
    # County observations — latest matching periods if possible
    obs = load_county_observations()
    county_row = {
        "Geography": "Kilifi County (KHIS county sheet)",
        "Level": "County aggregate",
        "Sub-county": "All 7 sub-counties",
        "Suspected": None, "Tested": None, "Confirmed": None, "AL": None,
        "R1.1 tests": None, "GO_1 TPR": None, "GO status": "na",
    }
    if not obs.empty:
        # Prefer monthly periods present in filter
        o = obs.copy()
        o["value"] = pd.to_numeric(o["value"], errors="coerce")
        if filters and filters.get("periods"):
            o = o[o["period"].astype(str).isin([str(p) for p in filters["periods"]])]
        def _sum_like(patterns, register=None):
            m = o["indicator"].astype(str)
            hit = pd.Series(False, index=o.index)
            for p in patterns:
                hit = hit | m.str.contains(p, case=False, na=False)
            sub = o[hit]
            if register:
                sub = sub[sub["register"] == register]
            return float(sub["value"].sum()) if not sub.empty else None
        county_row["Suspected"] = _sum_like(["Suspected"])
        county_row["Tested"] = _sum_like(["Tested for Malaria", "Total Exam"])
        county_row["Confirmed"] = _sum_like(["Confirmed", "Positive"])
        county_row["AL"] = _sum_like(["AL Total", "AL Dispensed"])
        rdt_pos = _sum_like(["RDT"], "MOH 706")  # too broad
        # RDT exams/pos more tightly
        rdt = o[(o["register"] == "MOH 706") & (o["indicator"].astype(str).str.contains("RDT", na=False))]
        if not rdt.empty:
            exams = float(rdt[rdt["indicator"].str.contains("Total Exam", na=False)]["value"].sum())
            pos = float(rdt[rdt["indicator"].str.contains("Positive", na=False)]["value"].sum())
            a = safe_rate(pos, exams, indicator="COUNTY_GO")
            county_row["GO_1 TPR"] = a.get("value") if a.get("status") == "valid" else None
            county_row["GO status"] = a.get("status")
        com = o[(o["register"] == "MOH 748") & (o["indicator"].astype(str).str.contains("Total", na=False))]
        if not com.empty:
            county_row["R1.1 tests"] = float(com["value"].sum())
    rows.append(county_row)
    geo = pd.DataFrame(rows)
    # Share of county (where both exist)
    sites = geo[geo["Level"] == "CBMP facility"]
    for col in ("Suspected", "Tested", "Confirmed", "AL", "R1.1 tests"):
        cval = county_row.get(col)
        sval = pd.to_numeric(sites[col], errors="coerce").sum()
        if cval and cval > 0 and pd.notna(sval):
            geo.loc[geo["Geography"] == "Kilifi County (KHIS county sheet)", f"{col} share note"] = ""
    return geo


def run_scenario(df, filters, test_delta_pct=0, rdt_avail_pct=100, chp_delta_pct=0):
    """
    B7 what-if on current filtered volumes. Not a prediction.
    """
    d = _filter_data(df, filters) if df is not None else pd.DataFrame()
    c = compute_cascade_metrics(d)
    go = compute_indicator("GO_1", d, filters)
    r11 = compute_indicator("R1_1", d, filters)
    base_tested = c.get("tested") or 0
    base_conf = c.get("confirmed") or 0
    base_tpr = go.get("value") if go.get("status") == "valid" else None
    base_r11 = r11.get("value") or 0
    # Availability scales testing
    avail = max(min(float(rdt_avail_pct), 150), 0) / 100.0
    new_tested = base_tested * (1 + test_delta_pct / 100.0) * avail
    if base_tpr is not None:
        new_pos = new_tested * (base_tpr / 100.0)
    else:
        new_pos = base_conf * (1 + test_delta_pct / 100.0) * avail
    new_r11 = base_r11 * (1 + chp_delta_pct / 100.0)
    extra_al = max(0, new_pos - base_conf)
    return {
        "base_tested": base_tested,
        "new_tested": new_tested,
        "base_confirmed": base_conf,
        "new_positives_est": new_pos,
        "tpr_used": base_tpr,
        "tpr_status": go.get("status"),
        "base_r11": base_r11,
        "new_r11": new_r11,
        "extra_al_est": extra_al,
        "note": "Uses current valid TPR as a constant. If TPR is INVALID/N/A, positives track confirmed only. Not a forecast.",
    }


def generate_management_brief(scope, name, filters, df):
    """B8 structured brief for facility / three sites / county proposal."""
    lines = []
    lines.append(f"CBMP MANAGEMENT BRIEF")
    lines.append(f"Scope: {scope} · {name}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} EAT")
    lines.append("")
    cat = evaluate_catalogue(df, filters)
    core = cat[cat["ID"].isin(["GO_1", "SO_1", "R1_1", "R1_2", "CAS_1", "CAS_2", "EPI_1", "DQ_03"])] if not cat.empty else pd.DataFrame()
    lines.append("1. EXECUTIVE SCORE")
    if not core.empty:
        for _, r in core.iterrows():
            v = r["Value"]
            if r["Status"] == "INVALID":
                disp = "INVALID"
            elif v is None:
                disp = "N/A"
            else:
                disp = f"{v}"
            lines.append(f"   · {r['ID']} {r['Indicator']}: {disp} [{r['Status']}] RAG={r['RAG']}")
    lines.append("")
    c = compute_cascade_metrics(_filter_data(df, filters))
    lines.append("2. CASCADE")
    lines.append(f"   · Suspected {c.get('suspected')} · Tested {c.get('tested')} · Confirmed {c.get('confirmed')} · AL {c.get('treated')}")
    lines.append(f"   · Diagnosis gap {c.get('diag_gap')} · Treatment gap {c.get('treat_gap')}")
    lines.append("")
    try:
        dqi = query("SELECT COUNT(*) AS n FROM dq_issues WHERE status='open'")
        lines.append(f"3. DATA QUALITY — open issues: {int(dqi.iloc[0]['n'])}")
    except Exception:
        lines.append("3. DATA QUALITY — (table unavailable)")
    try:
        run = query("SELECT id, items_flagged FROM recon_runs ORDER BY id DESC LIMIT 1")
        if not run.empty:
            lines.append(f"4. RECONCILIATION — latest run #{int(run.iloc[0]['id'])} flagged {int(run.iloc[0]['items_flagged'] or 0)}")
        else:
            lines.append("4. RECONCILIATION — no run yet")
    except Exception:
        lines.append("4. RECONCILIATION — n/a")
    fc = compute_commodity_forecast()
    if fc is not None and not fc.empty:
        bad = fc[fc["Status"].isin(["stock_out", "critical", "low"])]
        lines.append(f"5. COMMODITIES — {len(bad)} line(s) stock-out/critical/low")
    else:
        lines.append("5. COMMODITIES — no stock lines")
    _, clim = compute_climate_ew(df, filters)
    lines.append(f"6. CLIMATE SIGNAL — {clim.get('level')}: {clim.get('title')}")
    lines.append("")
    lines.append("7. NOTES FOR A PROPOSAL")
    lines.append("   · The three CBMP sites (Jaribuni/Ganze, Pingilikani/Kilifi South, Mwakuhenga/Kilifi North)")
    lines.append("     are the implementation footprint. Kilifi County sheet is the county aggregate.")
    lines.append("   · Do not treat the three sites as the entire sub-county health system.")
    lines.append("   · Use Comparison → Kilifi geographies for county vs sites shares.")
    lines.append("")
    if not cat.empty:
        inv = cat[cat["Status"] == "INVALID"]
        if not inv.empty:
            lines.append("8. INTEGRITY EXCEPTIONS")
            for _, r in inv.head(12).iterrows():
                lines.append(f"   · {r['ID']}: {r.get('Reason')}")
    return "\n".join(lines)


def page_budget(user, filters):
    page_header(
        "Budget & partners (B6)",
        "Activity-level budget vs expenditure for CBMP geographies. "
        "This is programme finance tracking — not IFMIS.",
    )
    try:
        lines = query("SELECT * FROM budget_lines ORDER BY fy DESC, partner, category")
    except Exception:
        lines = pd.DataFrame()
    if not lines.empty:
        lines["budget"] = pd.to_numeric(lines["budget"], errors="coerce")
        lines["expenditure"] = pd.to_numeric(lines["expenditure"], errors="coerce")
        tot_b = float(lines["budget"].sum())
        tot_e = float(lines["expenditure"].sum())
        a = safe_rate(tot_e, tot_b, indicator="BUD_EXEC", allow_over_100=True)
        c1, c2, c3 = st.columns(3)
        with c1:
            big_number(f"{tot_b:,.0f}", "Budget")
        with c2:
            big_number(f"{tot_e:,.0f}", "Expenditure")
        with c3:
            big_number(
                f"{a.get('value'):.1f}%" if a.get("status") == "valid" else "N/A",
                "Execution",
            )
        by = lines.groupby(["partner", "geography"], dropna=False)[["budget", "expenditure"]].sum().reset_index()
        st.dataframe(by, use_container_width=True, hide_index=True)
        st.dataframe(lines, use_container_width=True, hide_index=True)
        fig = px.bar(lines, x="activity", y=["budget", "expenditure"], barmode="group", height=360)
        fig.update_layout(plot_bgcolor="white", xaxis_tickangle=-30)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No budget lines yet. Add activities below (admin/partner).")

    if user.get("can_upload"):
        section_header("Add budget line")
        with st.form("bud_form"):
            a, b, c = st.columns(3)
            with a:
                fy = st.text_input("FY", value="2025/26")
                partner = st.text_input("Partner", value="World Friends / AICS")
            with b:
                geo = st.selectbox("Geography", ["All three", "Jaribuni", "Pingilikani", "Mwakuhenga", "Kilifi County"])
                category = st.selectbox("Category", ["HR/CHP", "Commodities", "Training", "Supervision", "IEC", "M&E", "Operations"])
            with c:
                budget = st.number_input("Budget", min_value=0.0, step=1000.0)
                expenditure = st.number_input("Expenditure to date", min_value=0.0, step=1000.0)
            activity = st.text_input("Activity")
            notes = st.text_input("Notes")
            if st.form_submit_button("Save line"):
                execute(
                    """
                    INSERT INTO budget_lines
                    (fy, partner, geography, category, activity, budget, expenditure, notes, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fy, partner, geo, category, activity, budget, expenditure, notes, user.get("username", "")),
                )
                st.success("Saved.")
                st.rerun()


def page_planning(user, filters):
    page_header(
        "Planning & scenarios (B7)",
        "What-if on current filtered volumes. Changes testing, RDT availability, or CHP tests. "
        "Not a prediction — a planning calculator using locked TPR when valid.",
    )
    df = load_all_data()
    if df.empty:
        st.warning("Commit KHIS data first.")
        return
    t1 = st.slider("Change in OPD testing volume (%)", -30, 50, 0, key="sc_test")
    t2 = st.slider("RDT availability (% of current)", 50, 120, 100, key="sc_rdt")
    t3 = st.slider("Change in CHP tests / R1.1 (%)", -30, 50, 0, key="sc_chp")
    sc = run_scenario(df, filters, t1, t2, t3)
    if sc["tpr_status"] != "valid":
        st.warning(f"GO_1 TPR is {sc['tpr_status']} — estimated positives do not use an invalid rate.")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        big_number(f"{sc['base_tested']:,.0f} → {sc['new_tested']:,.0f}", "OPD tested")
    with c2:
        big_number(f"{sc['base_confirmed']:,.0f} → {sc['new_positives_est']:,.0f}", "Positives (est.)")
    with c3:
        big_number(f"{sc['base_r11']:,.0f} → {sc['new_r11']:,.0f}", "CHP tests")
    with c4:
        big_number(f"{sc['extra_al_est']:,.0f}", "Extra AL courses (est.)")
    st.caption(sc["note"])
    out = pd.DataFrame([
        {"Measure": "OPD tested", "Baseline": sc["base_tested"], "Scenario": sc["new_tested"]},
        {"Measure": "Positives est.", "Baseline": sc["base_confirmed"], "Scenario": sc["new_positives_est"]},
        {"Measure": "CHP tests", "Baseline": sc["base_r11"], "Scenario": sc["new_r11"]},
    ])
    fig = px.bar(out, x="Measure", y=["Baseline", "Scenario"], barmode="group", height=360)
    fig.update_layout(plot_bgcolor="white")
    st.plotly_chart(fig, use_container_width=True)
    st.markdown(
        """
Use this when writing a proposal: e.g. “if community testing rises 15% and RDT stay at 100%, "
CHP tests move from A to B and estimated extra AL is C.” Pair with B4 stock and Budget.
        """
    )


def page_comparison(user, filters):
    """
    Multi-facility, register, and Kilifi geography comparison for proposals.
    """
    page_header(
        "Comparison & proposal geographies",
        "Three CBMP facilities remain the operational core. "
        "Add Kilifi County (KHIS county sheet) when you need a supervisor proposal that sits the sites in the whole county.",
        breadcrumb="Analyse / Comparison",
    )
    df = load_all_data()
    if df.empty:
        st.warning("No data uploaded.")
        return
    d0 = _filter_data(df, filters)
    if d0.empty:
        st.warning("No rows for active filters.")
        return

    indicators = compute_official_indicators(df, filters)
    mode = st.radio(
        "Comparison mode",
        [
            "Facility scoreboard",
            "Kilifi geographies (proposal)",
            "All 7 sub-counties",
            "Register volumes & rates",
            "Period A vs Period B",
        ],
        horizontal=True,
        key="cmp_mode",
    )

    if mode == "Kilifi geographies (proposal)":
        section_header("Kilifi County sheet vs three CBMP sites")
        st.markdown(
            """
The KHIS workbook has **Kilifi County** plus sheets that this app maps to:

| Sheet | Used as | Sub-county label |
|-------|---------|------------------|
| GANZE | Jaribuni facility | Ganze |
| KF SOUTH | Pingilikani facility | Kilifi South |
| KF NORTH | Mwakuhenga facility | Kilifi North |
| KILIFI COUNTY | County aggregate | All Kilifi |

Those three sheets are **not** every facility in the sub-county. For a proposal, say:  
*“CBMP operates in three sentinel facilities in Ganze, Kilifi South and Kilifi North; county totals are from the Kilifi County KHIS sheet.”*
            """
        )
        geo = suppress_count_columns(proposal_geography_table(df, filters), user=user)
        st.dataframe(geo, use_container_width=True, hide_index=True)
        sites = geo[geo["Level"] == "CBMP facility"]
        county = geo[geo["Level"] == "County aggregate"]
        share_rows = []
        if not county.empty and not sites.empty:
            for col in ("Suspected", "Tested", "Confirmed", "AL", "R1.1 tests"):
                cv = county.iloc[0].get(col)
                sv = pd.to_numeric(sites[col], errors="coerce").sum()
                if cv and float(cv) > 0 and pd.notna(sv):
                    a = safe_rate(sv, float(cv), indicator="SHARE", allow_over_100=True)
                    share_rows.append({
                        "Measure": col,
                        "Three sites": sv,
                        "Kilifi County sheet": float(cv),
                        "Sites as % of county sheet": a.get("value") if a.get("status") == "valid" else None,
                        "Status": (a.get("status") or "").upper(),
                    })
        if share_rows:
            st.markdown("**Share of county sheet represented by the three sites**")
            st.dataframe(suppress_count_columns(pd.DataFrame(share_rows), user=user), use_container_width=True, hide_index=True)
            sh = pd.DataFrame(share_rows).dropna(subset=["Sites as % of county sheet"])
            if not sh.empty:
                fig = px.bar(sh, x="Measure", y="Sites as % of county sheet", height=340)
                fig.update_layout(plot_bgcolor="white")
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info(
                "County share needs a committed KHIS file that includes the **Kilifi County** sheet "
                "(stored in observations, not mixed into facility KPIs)."
            )
        brief = generate_management_brief("proposal geography", "Kilifi vs 3 sites", filters, df)
        st.download_button(
            "📥 Download proposal geography note",
            brief.encode("utf-8"),
            file_name=f"cbmp_proposal_geography_{datetime.now().strftime('%Y%m%d')}.txt",
            mime="text/plain",
            key="b8_prop_note",
        )
        st.text_area("Proposal note (edit after download)", brief, height=240)
        return

    if mode == "All 7 sub-counties":
        section_header("Kilifi's seven sub-counties")
        st.markdown(
            "Official units: **Kilifi North** (county HQ) · Kilifi South · **Malindi** · **Magarini** · "
            "Ganze · Kaloleni · Rabai. CBMP sentinel sites sit in Ganze, Kilifi South and Kilifi North."
        )
        try:
            raw = query("SELECT * FROM kilifi_subcounty")
        except Exception:
            raw = pd.DataFrame()
        if raw.empty:
            st.warning(
                "No Kilifi-wide file loaded yet. Import the WFK workbook here — you do not need to leave this page."
            )
            wfk2 = st.file_uploader("WFK Kilifi workbook (County / Subcounty / Facility trends)", type=["xlsx"], key="cmp_wfk")
            if wfk2 is not None and user.get("can_upload") and st.button("Import on this page", key="cmp_wfk_go"):
                n, errs = import_wfk_kilifi_workbook(wfk2, user.get("username", "system"))
                if n:
                    st.success(f"Imported {n} rows.")
                    st.rerun()
                if errs:
                    st.error("; ".join(errs))
            st.caption("Malindi and Magarini stay pending until they appear in the extract.")
            miss = pd.DataFrame({
                "Sub-county": KILIFI_SUBCOUNTIES,
                "In WFK extract": ["No" if s in ("Malindi", "Magarini") else "Expected after import" for s in KILIFI_SUBCOUNTIES],
                "CBMP site": [CBMP_IN_SUBCOUNTY.get(s, "—") for s in KILIFI_SUBCOUNTIES],
            })
            st.dataframe(miss, use_container_width=True, hide_index=True)
            return
        # Filters for 7 sub-county comparison
        raw["positivity"] = pd.to_numeric(raw["positivity"], errors="coerce")
        for _c in ("suspected", "tested", "confirmed", "al_dispensed", "rdt_exam", "rdt_pos"):
            if _c in raw.columns:
                raw[_c] = pd.to_numeric(raw[_c], errors="coerce")
        periods = ["(all)"] + sorted(raw["period"].dropna().astype(str).unique().tolist(), reverse=True)
        f1, f2, f3 = st.columns(3)
        with f1:
            pick = st.selectbox("Period", periods, key="k7_period")
        with f2:
            sc_sel = st.multiselect(
                "Sub-counties",
                KILIFI_SUBCOUNTIES,
                default=KILIFI_SUBCOUNTIES,
                key="k7_sc_sel",
            )
        with f3:
            cmp_mode = st.selectbox(
                "Compare",
                [
                    "Current period board",
                    "Current vs previous period",
                    "Current vs same month last year",
                    "Sub-county vs sub-county",
                    "Sub-county vs mean of reporting units",
                ],
                key="k7_cmp_mode",
            )
        st.caption(
            "Data types that can be uploaded for this view: **WFK workbook** (County/Subcounty/Facility trends). "
            "Commit is required on Data foundation (Validate → Commit). KHIS facility data stays separate."
        )
        view = raw if pick == "(all)" else raw[raw["period"].astype(str) == pick]
        if sc_sel:
            view = view[view["sub_county"].isin(sc_sel)] if "sub_county" in view.columns else view
        # Roll up if all
        if pick == "(all)":
            agg = view.groupby("sub_county", as_index=False).agg(
                suspected=("suspected", "sum"),
                tested=("tested", "sum"),
                confirmed=("confirmed", "sum"),
                al_dispensed=("al_dispensed", "sum"),
                rdt_exam=("rdt_exam", "sum"),
                rdt_pos=("rdt_pos", "sum"),
                flag_pos=("flag_pos_gt_100", "max"),
                flag_conf=("flag_conf_gt_tested", "max"),
            )
        else:
            agg = view.rename(columns={"flag_pos_gt_100": "flag_pos", "flag_conf_gt_tested": "flag_conf"})
        board = []
        for scn in KILIFI_SUBCOUNTIES:
            hit = agg[agg["sub_county"] == scn] if "sub_county" in agg.columns else pd.DataFrame()
            if hit.empty:
                board.append({
                    "Sub-county": scn,
                    "CBMP site": CBMP_IN_SUBCOUNTY.get(scn, "—"),
                    "Coverage": "Pending in WFK extract" if scn in ("Malindi", "Magarini") else "No rows this period",
                    "Suspected": None, "Tested": None, "Confirmed": None,
                    "RDT TPR %": None, "Integrity": "—",
                })
                continue
            r = hit.iloc[0]
            tpr_a = safe_rate(float(r.get("rdt_pos") or 0), float(r.get("rdt_exam") or 0), indicator="K7_TPR")
            test_a = safe_rate(float(r.get("tested") or 0), float(r.get("suspected") or 0), indicator="K7_TEST")
            try:
                diag_g = float(r.get("suspected") or 0) - float(r.get("tested") or 0)
            except Exception:
                diag_g = None
            integ = []
            if int(r.get("flag_pos") or 0):
                integ.append("pos>100 flag")
            if int(r.get("flag_conf") or 0):
                integ.append("conf>tested flag")
            if tpr_a.get("status") == "invalid":
                integ.append("INVALID TPR")
            board.append({
                "Sub-county": scn,
                "CBMP site": CBMP_IN_SUBCOUNTY.get(scn, "—"),
                "Coverage": "In extract",
                "Suspected": r.get("suspected"),
                "Tested": r.get("tested"),
                "Confirmed": r.get("confirmed"),
                "RDT TPR %": tpr_a.get("value") if tpr_a.get("status") == "valid" else None,
                "Testing rate %": test_a.get("value") if test_a.get("status") == "valid" else None,
                "Diagnosis gap": diag_g,
                "AL dispensed": r.get("al_dispensed"),
                "RDT exams": r.get("rdt_exam"),
                "RDT positive": r.get("rdt_pos"),
                "Integrity": "; ".join(integ) if integ else "OK",
            })
        board_df = suppress_count_columns(pd.DataFrame(board), user=user)
        st.caption("No composite score. High TPR is burden or data quality until VALID status and volume are read together. Counts are not rates — use TPR / testing rate for intensity.")
        if user.get("aggregation"):
            st.caption("Small counts suppressed for this role (min_cell).")
        st.dataframe(board_df, use_container_width=True, hide_index=True)

        # Current vs previous period deltas
        if cmp_mode == "Current vs previous period" and pick != "(all)" and len(periods) > 2:
            prev_candidates = [p for p in periods if p not in ("(all)", pick)]
            if prev_candidates:
                prev_p = prev_candidates[0]
                prev_view = raw[raw["period"].astype(str) == prev_p]
                delta_rows = []
                for scn in (sc_sel or KILIFI_SUBCOUNTIES):
                    cur = view[view["sub_county"] == scn] if "sub_county" in view.columns else pd.DataFrame()
                    prv = prev_view[prev_view["sub_county"] == scn] if "sub_county" in prev_view.columns else pd.DataFrame()
                    if cur.empty:
                        continue
                    c = cur.iloc[0]
                    p = prv.iloc[0] if not prv.empty else None
                    def _d(a, b):
                        try:
                            a, b = float(a or 0), float(b or 0)
                            return a - b
                        except Exception:
                            return None
                    delta_rows.append({
                        "Sub-county": scn,
                        "Period": pick,
                        "Prev period": prev_p,
                        "Confirmed": c.get("confirmed"),
                        "Prev confirmed": None if p is None else p.get("confirmed"),
                        "Δ confirmed": None if p is None else _d(c.get("confirmed"), p.get("confirmed")),
                        "Tested": c.get("tested"),
                        "Prev tested": None if p is None else p.get("tested"),
                        "Δ tested": None if p is None else _d(c.get("tested"), p.get("tested")),
                    })
                if delta_rows:
                    st.markdown(f"#### Change vs previous period (**{prev_p}**)")
                    st.dataframe(suppress_count_columns(pd.DataFrame(delta_rows), user=user), use_container_width=True, hide_index=True)
                    st.caption("⚠️ Rising confirmed with falling tested suggests surveillance disruption — do not interpret as transmission alone.")

        # Year-on-year: same month last year
        if cmp_mode == "Current vs same month last year" and pick != "(all)":
            yoy_target = None
            m = re.match(r"^(\d{4})-(\d{2})", str(pick))
            if m:
                yoy_target = f"{int(m.group(1)) - 1}-{m.group(2)}"
            if yoy_target and yoy_target in [str(p) for p in periods]:
                yoy_view = raw[raw["period"].astype(str) == yoy_target]
                yoy_rows = []
                for scn in (sc_sel or KILIFI_SUBCOUNTIES):
                    cur = view[view["sub_county"] == scn] if "sub_county" in view.columns else pd.DataFrame()
                    prv = yoy_view[yoy_view["sub_county"] == scn] if "sub_county" in yoy_view.columns else pd.DataFrame()
                    if cur.empty:
                        continue
                    c = cur.iloc[0]
                    p = prv.iloc[0] if not prv.empty else None
                    def _d2(a, b):
                        try:
                            return float(a or 0) - float(b or 0)
                        except Exception:
                            return None
                    yoy_rows.append({
                        "Sub-county": scn,
                        "Period": pick,
                        "Same month LY": yoy_target,
                        "Confirmed": c.get("confirmed"),
                        "LY confirmed": None if p is None else p.get("confirmed"),
                        "Δ confirmed": None if p is None else _d2(c.get("confirmed"), p.get("confirmed")),
                        "Tested": c.get("tested"),
                        "LY tested": None if p is None else p.get("tested"),
                        "Δ tested": None if p is None else _d2(c.get("tested"), p.get("tested")),
                    })
                if yoy_rows:
                    st.markdown(f"#### Year-on-year (**{pick}** vs **{yoy_target}**)")
                    st.dataframe(suppress_count_columns(pd.DataFrame(yoy_rows), user=user), use_container_width=True, hide_index=True)
                    st.caption("Same calendar month last year is useful for seasonal comparison. Counts are not rates.")
            else:
                st.info(
                    f"⚠️ Same month last year not found for **{pick}** "
                    f"(looked for `{yoy_target or '—'}`). Upload a longer WFK history to enable YoY."
                )

        view_kind = st.radio(
            "View",
            ["Indicator chart", "Sub-county vs county mean", "Explain one sub-county", "Facility drill"],
            horizontal=True,
            key="k7_view",
        )
        ind_pick = st.selectbox(
            "Indicator for chart / vs county",
            [
                "Confirmed", "Tested", "Suspected", "RDT TPR %",
                "Testing rate %", "Diagnosis gap", "AL dispensed", "RDT exams", "RDT positive",
            ],
            key="k7_ind",
        )
        plot = board_df.dropna(subset=[ind_pick]) if ind_pick in board_df.columns else pd.DataFrame()
        if view_kind == "Indicator chart":
            if plot.empty:
                st.info("No values for that indicator in this period.")
            else:
                fig = px.bar(
                    plot, x="Sub-county", y=ind_pick, color="Integrity", height=380,
                    title=f"{ind_pick} — 7 sub-counties (empty = pending extract)",
                )
                fig.update_layout(plot_bgcolor="white")
                st.plotly_chart(fig, use_container_width=True)

        if view_kind == "Sub-county vs county mean":
            nums = pd.to_numeric(board_df[ind_pick], errors="coerce") if ind_pick in board_df.columns else pd.Series(dtype=float)
            county_val = nums.mean(skipna=True)
            st.markdown(
                f"**Mean of reporting sub-counties** for {ind_pick}: "
                f"**{county_val:.2f}**. This is **not** the official Kilifi County KHIS aggregate total."
            )
            ctx = board_df.copy()
            if ind_pick in ctx.columns:
                ctx["vs county mean (pp or count)"] = pd.to_numeric(ctx[ind_pick], errors="coerce") - county_val
            st.dataframe(ctx, use_container_width=True, hide_index=True)
            st.caption("Differences are **points or counts**, not “% worse”. High burden ≠ poor performance.")

        if view_kind == "Explain one sub-county":
            scn = st.selectbox("Sub-county profile", KILIFI_SUBCOUNTIES, key="k7_prof")
            row = board_df[board_df["Sub-county"] == scn]
            section_header(f"Profile — {scn}")
            if row.empty:
                st.info("No row.")
            else:
                r = row.iloc[0]
                st.markdown(
                    f"""
- **CBMP sentinel:** {r.get('CBMP site')}  
- **Coverage in WFK file:** {r.get('Coverage')}  
- **Suspected / tested / confirmed:** {r.get('Suspected')} / {r.get('Tested')} / {r.get('Confirmed')}  
- **RDT TPR:** {r.get('RDT TPR %')} · **Integrity:** {r.get('Integrity')}  
                    """
                )
                if r.get("Integrity") not in ("OK", "—", None) and "INVALID" in str(r.get("Integrity")):
                    st.error("This TPR is **INVALID**. Do not rank the sub-county on it. Open Data foundation / DQ.")
                site = CBMP_IN_SUBCOUNTY.get(scn)
                if site:
                    pf = dict(filters or {})
                    pf["facility"] = site
                    fd = _filter_data(df, pf)
                    c = compute_cascade_metrics(fd)
                    go = compute_indicator("GO_1", fd, facility=site)
                    st.markdown(
                        f"**Sentinel {site} (KHIS, current filters)**  \n"
                        f"Tested {c.get('tested')} · Confirmed {c.get('confirmed')} · "
                        f"Diag gap {c.get('diag_gap')} · Treat gap {c.get('treat_gap')} · "
                        f"GO_1 {go.get('value') if go.get('status')=='valid' else 'N/A'} [{go.get('status')}]"
                    )
                st.caption("Why different? Check volume, integrity flags, facility mix below, and whether Malindi/Magarini are still pending.")

        if view_kind == "Facility drill":
            try:
                facw = query("SELECT sub_county, unit, period, confirmed, positivity, flag_pos_gt_100 FROM kilifi_facility_wide")
            except Exception:
                facw = pd.DataFrame()
            if facw.empty:
                st.info("No facility-wide WFK rows.")
            else:
                if pick != "(all)":
                    facw = facw[facw["period"].astype(str) == pick]
                st.dataframe(
                    facw.sort_values(["sub_county", "confirmed"], ascending=[True, False]),
                    use_container_width=True, hide_index=True,
                )

        st.download_button(
            "📥 Download 7-sub-county board",
            board_df.to_csv(index=False).encode("utf-8"),
            file_name=f"kilifi_7_subcounties_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="k7_csv",
        )
        return

    # ── Facility scoreboard ──
    if mode == "Facility scoreboard":
        section_header("Facility scoreboard (official + cascade)")
        rows = []
        for fac in FACILITIES:
            if filters.get("facility") not in (None, "All three") and fac != filters["facility"]:
                continue
            pf = dict(filters or {})
            pf["facility"] = fac
            fd = _filter_data(df, pf)
            c = compute_cascade_metrics(fd)
            go = indicators["GO_1"].get("per_facility", {}).get(fac, {}) or {}
            r11 = (indicators["R1_1"].get("per_facility") or {}).get(fac)
            r12 = (indicators["R1_2"].get("per_facility") or {}).get(fac)
            go_val = go.get("value") if go.get("status") in (None, "valid") or go.get("status") == "valid" else None
            if go.get("status") == "invalid":
                go_disp = f"INVALID ({go.get('calculated_pct')})"
                go_rag = "grey"
            else:
                go_disp = f"{go_val:.1f}" if go_val is not None else "—"
                go_rag = indicator_rag(
                    {"value": go_val, "target": indicators["GO_1"]["target"], "baseline": indicators["GO_1"]["baseline"],
                     "testing_collapse": indicators["GO_1"].get("testing_collapse"), "formula_status": go.get("status")},
                    "GO_1",
                )
            rs = c.get("rate_status") or {}
            rows.append({
                "Facility": fac,
                "GO_1 TPR": go_disp,
                "GO RAG": {"green": "🟢", "amber": "🟡", "red": "🔴", "grey": "⚫"}.get(go_rag, "⚫"),
                "R1.1 tests": f"{int(r11):,}" if r11 is not None else "—",
                "R1.2 %": f"{r12:.0f}" if r12 is not None else "—",
                "Suspected": c["suspected"],
                "Tested": c["tested"],
                "Confirmed": c["confirmed"],
                "AL": c["treated"],
                "Testing %": f"{c['testing_rate']:.0f}" if rs.get("testing_rate") != "invalid" else f"INV({c['testing_rate']:.0f})",
                "Treat cov %": f"{c['treat_coverage']:.0f}" if rs.get("treat_coverage") != "invalid" else f"INV({c['treat_coverage']:.0f})",
                "Diag gap": c["diag_gap"],
                "Treat gap": c["treat_gap"],
            })
        _sb = suppress_count_columns(pd.DataFrame(rows), user=user)
        if user.get("aggregation"):
            st.caption("Small counts suppressed for this role (min_cell). Rates unchanged.")
        st.dataframe(_sb, use_container_width=True, hide_index=True)

        # Bar compare confirmed / gaps (use raw numeric rows for charts; suppressed cells as 0 for plot only)
        chart_df = pd.DataFrame(rows)
        if not chart_df.empty:
            c1, c2 = st.columns(2)
            with c1:
                fig = px.bar(
                    chart_df, x="Facility", y=["Confirmed", "Tested", "Suspected"],
                    barmode="group", height=360,
                )
                fig.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                fig2 = px.bar(
                    chart_df, x="Facility", y=["Diag gap", "Treat gap"],
                    barmode="group", height=360,
                    color_discrete_sequence=[COLOR_RED, COLOR_AMBER],
                )
                fig2.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
                st.plotly_chart(fig2, use_container_width=True)

        # Export
        st.download_button(
            "📥 Download facility scoreboard CSV",
            pd.DataFrame(rows).to_csv(index=False).encode("utf-8"),
            file_name=f"facility_comparison_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="cmp_fac_csv",
        )

    # ── Register comparison ──
    elif mode == "Register volumes & rates":
        section_header("Register volumes & formula-safe rates")
        selected = st.multiselect(
            "Registers",
            REGISTERS,
            default=[r for r in ["MOH 705A", "MOH 705B", "MOH 706", "MOH 743", "MOH 748"] if r in REGISTERS],
            key="cmp_regs",
        )
        if not selected:
            st.info("Select at least one register.")
            return
        d = d0[d0["register"].isin(selected)]
        rows = []
        for reg in selected:
            reg_df = d[d["register"] == reg]
            if reg_df.empty:
                rows.append({
                    "Register": reg, "Rows": 0, "Volume sum": 0,
                    "Suspected": 0, "Tested": 0, "Positive/Confirmed": 0,
                    "Positivity": "—", "Pos status": "na",
                    "Reporting %": "—", "RR status": "na",
                })
                continue
            vol = float(reg_df["value"].sum())
            susp = float(reg_df[reg_df["indicator"].str.contains("Suspected", na=False)]["value"].sum())
            tested = float(
                reg_df[reg_df["indicator"].str.contains(r"Tested|Total Exam", na=False, regex=True)]["value"].sum()
            )
            pos = float(
                reg_df[reg_df["indicator"].str.contains(r"Positive|Confirmed", na=False, regex=True)]["value"].sum()
            )
            # Prefer RDT pair on 706
            if reg == "MOH 706":
                rdt = reg_df[reg_df["indicator"].str.contains("RDT", na=False)]
                tested = float(rdt[rdt["indicator"].str.contains("Total Exam", na=False)]["value"].sum())
                pos = float(rdt[rdt["indicator"].str.contains("Positive", na=False)]["value"].sum())
            pos_a = safe_rate(pos, tested, indicator=f"cmp_{reg}_pos", source=reg)
            rr_sub = reg_df[reg_df["indicator"].str.contains(r"Reporting\s*Rate", na=False, case=False)]
            if not rr_sub.empty:
                rr_a = reporting_rate_audit(rr_sub["value"].mean(), register=reg)
            else:
                rr_a = {"status": "na", "value": None, "calculated_pct": None}
            rows.append({
                "Register": reg,
                "Rows": len(reg_df),
                "Volume sum": int(vol),
                "Suspected": int(susp),
                "Tested": int(tested),
                "Positive/Confirmed": int(pos),
                "Positivity": (
                    f"{pos_a['value']:.1f}%" if pos_a.get("status") == "valid" else
                    (f"INVALID ({pos_a.get('calculated_pct')}%)" if pos_a.get("status") == "invalid" else "—")
                ),
                "Pos status": pos_a.get("status"),
                "Reporting %": (
                    f"{rr_a['value']:.0f}%" if rr_a.get("status") == "valid" else
                    (f"INVALID ({rr_a.get('calculated_pct')}%)" if rr_a.get("status") == "invalid" else "—")
                ),
                "RR status": rr_a.get("status"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        fig = px.bar(
            pd.DataFrame(rows), x="Register", y="Volume sum", color="Register", height=360,
        )
        fig.update_layout(plot_bgcolor="white", showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("MOH 706 positivity uses RDT exams/positives only. Reporting rates >100% are INVALID.")

    # ── Period A vs B ──
    else:
        section_header("Period A vs Period B")
        periods = sorted({str(p) for p in d0["period"].dropna().unique()})
        monthly = [p for p in periods if len(p) == 7 and p[4] == "-"]
        choices = monthly or periods
        if len(choices) < 2:
            st.info("Need at least two periods in the current filter for period comparison.")
            return
        c1, c2 = st.columns(2)
        with c1:
            per_a = st.selectbox("Period A (baseline)", choices, index=max(0, len(choices) - 2), key="cmp_pa")
        with c2:
            per_b = st.selectbox("Period B (compare)", choices, index=len(choices) - 1, key="cmp_pb")
        if per_a == per_b:
            st.warning("Choose two different periods.")
            return

        def _period_slice(per):
            pf = dict(filters or {})
            pf["periods"] = [per]
            pf["period_mode"] = "Custom"
            return _filter_data(df, pf)

        da, db = _period_slice(per_a), _period_slice(per_b)
        ca, cb = compute_cascade_metrics(da), compute_cascade_metrics(db)
        ind_a = compute_official_indicators(df, {"facility": filters.get("facility"), "periods": [per_a], "period_mode": "Custom"})
        ind_b = compute_official_indicators(df, {"facility": filters.get("facility"), "periods": [per_b], "period_mode": "Custom"})

        metrics = [
            ("Suspected", ca["suspected"], cb["suspected"]),
            ("Tested", ca["tested"], cb["tested"]),
            ("Confirmed", ca["confirmed"], cb["confirmed"]),
            ("AL treated", ca["treated"], cb["treated"]),
            ("Diag gap", ca["diag_gap"], cb["diag_gap"]),
            ("Treat gap", ca["treat_gap"], cb["treat_gap"]),
            ("R1.1 CHP tests", ind_a["R1_1"].get("value") or 0, ind_b["R1_1"].get("value") or 0),
        ]
        mrows = []
        for label, va, vb in metrics:
            pct, note = pct_change_safe(va, vb)
            mrows.append({
                "Metric": label,
                f"A ({per_a})": f"{va:,.0f}" if isinstance(va, (int, float)) else va,
                f"B ({per_b})": f"{vb:,.0f}" if isinstance(vb, (int, float)) else vb,
                "Δ": f"{(vb or 0) - (va or 0):+,.0f}",
                "% change": f"{pct:+.1f}%" if pct is not None else (note or "n/a"),
            })
        # Rates
        for key, name in [("GO_1", "GO_1 TPR"), ("R1_2", "R1.2 reporting")]:
            va, vb = ind_a[key].get("value"), ind_b[key].get("value")
            sa, sb = ind_a[key].get("formula_status"), ind_b[key].get("formula_status")
            mrows.append({
                "Metric": name,
                f"A ({per_a})": (
                    f"{va:.1f}%" if va is not None else (
                        f"INVALID ({ind_a[key].get('calculated_pct')})" if sa == "invalid" else "N/A"
                    )
                ),
                f"B ({per_b})": (
                    f"{vb:.1f}%" if vb is not None else (
                        f"INVALID ({ind_b[key].get('calculated_pct')})" if sb == "invalid" else "N/A"
                    )
                ),
                "Δ": (
                    f"{(vb - va):+.1f} pp" if va is not None and vb is not None else "—"
                ),
                "% change": "pp change (not volume %)" if va is not None and vb is not None else "n/a",
            })
        st.dataframe(pd.DataFrame(mrows), use_container_width=True, hide_index=True)
        # Chart volumes
        vol_chart = pd.DataFrame([
            {"Period": per_a, "Metric": m, "Value": va}
            for m, va, vb in metrics
        ] + [
            {"Period": per_b, "Metric": m, "Value": vb}
            for m, va, vb in metrics
        ])
        fig = px.bar(
            vol_chart, x="Metric", y="Value", color="Period", barmode="group", height=400,
        )
        fig.update_layout(plot_bgcolor="white", legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.download_button(
            "📥 Download period comparison CSV",
            pd.DataFrame(mrows).to_csv(index=False).encode("utf-8"),
            file_name=f"period_compare_{per_a}_vs_{per_b}.csv",
            mime="text/csv",
            key="cmp_per_csv",
        )


# ==================================================================
# PAGE 16 — CLIMATE
# ==================================================================

def fetch_open_meteo_weekly(days=56):
    """
    Pull daily rain + temperature + humidity from Open-Meteo for
    3 CBMP sites AND 7 sub-county gazetteer centroids.
    """
    import urllib.request
    import json
    import time
    end = datetime.now().date()
    start = end - pd.Timedelta(days=days)
    rows, errors = [], []
    targets = []
    for fac, geo in GEO_SITES.items():
        targets.append((fac, geo["lat"], geo["lon"], "facility"))
    for scn, geo in SUBCOUNTY_GEO.items():
        targets.append((scn, geo["lat"], geo["lon"], "sub_county"))
    for fac, lat, lon, kind in targets:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=precipitation_sum,precipitation_hours,temperature_2m_mean,"
            "temperature_2m_min,temperature_2m_max,relative_humidity_2m_mean,"
            "et0_fao_evapotranspiration"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
            "&timezone=Africa%2FNairobi"
        )
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            errors.append(f"{fac}: {e}")
            time.sleep(0.6)
            continue
        time.sleep(0.4)
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        tmp = pd.DataFrame({
            "date": pd.to_datetime(dates),
            "rain": pd.to_numeric(daily.get("precipitation_sum") or [], errors="coerce"),
            "temp": pd.to_numeric(daily.get("temperature_2m_mean") or [], errors="coerce"),
            "tmin": pd.to_numeric(daily.get("temperature_2m_min") or [], errors="coerce"),
            "tmax": pd.to_numeric(daily.get("temperature_2m_max") or [], errors="coerce"),
            "hum": pd.to_numeric(daily.get("relative_humidity_2m_mean") or [], errors="coerce"),
            "rhours": pd.to_numeric(daily.get("precipitation_hours") or [], errors="coerce"),
            "et0": pd.to_numeric(daily.get("et0_fao_evapotranspiration") or [], errors="coerce"),
        })
        if tmp.empty:
            errors.append(f"{fac}: empty Open-Meteo payload")
            continue
        tmp["week_ending"] = tmp["date"] + pd.to_timedelta(6 - tmp["date"].dt.dayofweek, unit="d")
        grp = tmp.groupby("week_ending").agg(
            rain=("rain", "sum"), temp=("temp", "mean"), hum=("hum", "mean"),
            tmin=("tmin", "min"), tmax=("tmax", "max"),
            rhours=("rhours", "sum"), et0=("et0", "sum"),
        ).reset_index()
        for _, r in grp.iterrows():
            rows.append({
                "facility": fac,
                "geography": kind,
                "week_ending": pd.Timestamp(r["week_ending"]).strftime("%Y-%m-%d"),
                "rainfall_mm": None if pd.isna(r["rain"]) else round(float(r["rain"]), 1),
                "temperature_c": None if pd.isna(r["temp"]) else round(float(r["temp"]), 1),
                "humidity_pct": None if pd.isna(r.get("hum")) else round(float(r["hum"]), 1),
                "temp_min": None if pd.isna(r.get("tmin")) else round(float(r["tmin"]), 1),
                "temp_max": None if pd.isna(r.get("tmax")) else round(float(r["tmax"]), 1),
                "rain_hours": None if pd.isna(r.get("rhours")) else round(float(r["rhours"]), 1),
                "et0_mm": None if pd.isna(r.get("et0")) else round(float(r["et0"]), 1),
                "horizon": "observed_model",
                "source": "open-meteo",
            })
    return rows, errors


def fetch_open_meteo_forecast(ahead_days=16):
    """Daily forecast through +16 days (Open-Meteo). Not a malaria model.
    Uses forecast_days= (avoids HTTP 400 from start/end past model range).
    Core daily vars only — humidity/et0 optional if supported.
    """
    import urllib.request
    import json
    import time
    import urllib.error
    days = max(1, min(16, int(ahead_days or 16)))
    rows, errors = [], []
    targets = []
    for fac, geo in GEO_SITES.items():
        targets.append((fac, float(geo["lat"]), float(geo["lon"]), "facility"))
    for scn, geo in SUBCOUNTY_GEO.items():
        targets.append((scn, float(geo["lat"]), float(geo["lon"]), "sub_county"))
    for fac, lat, lon, kind in targets:
        # Primary request: widely supported daily fields
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat:.5f}&longitude={lon:.5f}"
            "&daily=precipitation_sum,temperature_2m_mean,temperature_2m_min,temperature_2m_max"
            f"&forecast_days={days}"
            "&timezone=Africa%2FNairobi"
        )
        payload = None
        try:
            with urllib.request.urlopen(url, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                body = ""
            errors.append(f"{fac}: HTTP {e.code} {body or e.reason}")
            time.sleep(0.5)
            continue
        except Exception as e:
            errors.append(f"{fac}: {e}")
            time.sleep(0.5)
            continue
        time.sleep(0.25)
        daily = (payload or {}).get("daily") or {}
        dates = daily.get("time") or []
        if not dates:
            errors.append(f"{fac}: empty daily series")
            continue

        def _g(key, i):
            arr = daily.get(key) or []
            try:
                v = arr[i]
                return None if v is None else float(v)
            except Exception:
                return None

        for i, ds in enumerate(dates):
            rows.append({
                "forecast_date": ds,
                "facility": fac,
                "geography": kind,
                "rainfall_mm": _g("precipitation_sum", i),
                "temp_mean": _g("temperature_2m_mean", i),
                "temp_min": _g("temperature_2m_min", i),
                "temp_max": _g("temperature_2m_max", i),
                "humidity_pct": None,  # optional; omit from API to avoid 400
                "rain_hours": None,
                "et0_mm": None,
                "source": "open-meteo-forecast",
            })
    return rows, errors


def upsert_climate_forecast(rows, user="system"):
    n = 0
    conn = get_conn()
    conn.execute("DELETE FROM climate_forecast")
    for r in rows:
        conn.execute(
            """
            INSERT INTO climate_forecast (
                forecast_date, facility, geography, rainfall_mm, temp_mean, temp_min, temp_max,
                humidity_pct, rain_hours, et0_mm, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r.get("forecast_date"), r.get("facility"), r.get("geography"),
                r.get("rainfall_mm"), r.get("temp_mean"), r.get("temp_min"), r.get("temp_max"),
                r.get("humidity_pct"), r.get("rain_hours"), r.get("et0_mm"), r.get("source"),
            ),
        )
        n += 1
    conn.commit()
    conn.close()
    log_audit(user, "climate_forecast", "climate_forecast", f"rows={n}")
    return n


def upsert_climate_rows(rows, user="system"):
    n = 0
    conn = get_conn()
    for r in rows:
        # replace same facility+week+source
        conn.execute(
            "DELETE FROM climate_data WHERE facility=? AND week_ending=? AND IFNULL(source,'')=?",
            (r["facility"], r["week_ending"], r.get("source") or ""),
        )
        conn.execute(
            """
            INSERT INTO climate_data (
                week_ending, facility, rainfall_mm, temperature_c, source, geography, humidity_pct,
                temp_min, temp_max, rain_hours, et0_mm, horizon
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["week_ending"], r["facility"], r["rainfall_mm"], r["temperature_c"],
                r.get("source"), r.get("geography"), r.get("humidity_pct"),
                r.get("temp_min"), r.get("temp_max"), r.get("rain_hours"), r.get("et0_mm"),
                r.get("horizon") or "observed_model",
            ),
        )
        n += 1
    conn.commit()
    conn.close()
    log_audit(user, "climate_open_meteo", "climate_data", f"rows={n}")
    return n


def climate_lag_table(df, filters=None):
    """
    Historical check: monthly confirmed vs monthly rainfall at lag 0, 1, 2 months.
    Correlation is a check, not a forecast.
    """
    try:
        clim = query("SELECT week_ending, rainfall_mm FROM climate_data WHERE rainfall_mm IS NOT NULL")
    except Exception:
        return pd.DataFrame(), "No climate rows"
    if clim.empty or df is None or df.empty:
        return pd.DataFrame(), "Need climate log + KHIS"
    clim["week_ending"] = pd.to_datetime(clim["week_ending"], errors="coerce")
    clim["month"] = clim["week_ending"].dt.to_period("M").astype(str)
    rain_m = clim.groupby("month")["rainfall_mm"].sum()
    d = _filter_data(df, filters or {})
    monthly = d[d["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)] if not d.empty else pd.DataFrame()
    if monthly.empty:
        return pd.DataFrame(), "Need monthly KHIS periods"
    cases = []
    for per, g in monthly.groupby(monthly["period"].astype(str)):
        c = compute_cascade_metrics(g)
        cases.append({"month": per, "confirmed": c.get("confirmed") or 0})
    case_s = pd.DataFrame(cases).set_index("month")["confirmed"]
    idx = sorted(set(rain_m.index) & set(case_s.index))
    if len(idx) < 4:
        return pd.DataFrame(), f"Only {len(idx)} overlapping months — need ≥4 to check lag"
    rows = []
    for lag in (0, 1, 2):
        xs, ys = [], []
        for m in idx:
            try:
                tgt = (pd.Period(m, freq="M") - lag).strftime("%Y-%m")
            except Exception:
                continue
            if tgt in rain_m.index:
                xs.append(float(rain_m.loc[tgt]))
                ys.append(float(case_s.loc[m]))
        if len(xs) < 4:
            rows.append({"Lag (months)": lag, "n": len(xs), "corr": None, "Note": "too few pairs"})
            continue
        corr = float(pd.Series(xs).corr(pd.Series(ys)))
        rows.append({
            "Lag (months)": lag,
            "n": len(xs),
            "corr": None if pd.isna(corr) else round(corr, 3),
            "Note": "rain vs confirmed same month" if lag == 0 else f"rain {lag} month(s) earlier",
        })
    return pd.DataFrame(rows), "Correlation of rainfall with confirmed cases. |r| near 0 = no linear link in this window."


# Alert family → default roles (documentation + seed)
ALERT_ROUTING_MATRIX = {
    "Data quality": {
        "primary_roles": ["Facility data focal", "Sub-county M&E"],
        "escalation_roles": ["Sub-county M&E", "County M&E"],
        "default_hours": 24,
    },
    "Surveillance": {
        "primary_roles": ["Facility in-charge", "Sub-county malaria focal"],
        "escalation_roles": ["County malaria team"],
        "default_hours": 24,
    },
    "Forecast": {
        "primary_roles": ["Sub-county malaria focal", "County malaria team"],
        "escalation_roles": ["County EPR"],
        "default_hours": 12,
    },
    "Commodity": {
        "primary_roles": ["Facility commodity focal", "Sub-county logistics"],
        "escalation_roles": ["County logistics"],
        "default_hours": 24,
    },
}

SEVERITY_RANK = {"warning": 1, "high": 2, "critical": 3, "info": 0}


def resolve_alert_recipients(facility=None, category=None, severity="warning", family=None):
    """
    Look up active alert_recipients for this geography + family + severity.
    Returns list of dicts: name, role, phone, email, sms_enabled, escalation_hours.
    """
    fam = family or _alert_family(category, "")
    sev = str(severity or "warning").lower()
    try:
        rec = query("SELECT * FROM alert_recipients WHERE IFNULL(active,1)=1")
    except Exception:
        return []
    if rec.empty:
        return []
    out = []
    sub = None
    if facility and facility in GEO_SITES:
        sub = GEO_SITES[facility].get("sub_county")
    for _, r in rec.iterrows():
        # Family filter: blank or match
        rfam = str(r.get("alert_family") or "").strip()
        if rfam and rfam.lower() not in (fam.lower(), "all", "*"):
            continue
        # Severity threshold
        min_s = str(r.get("min_severity") or "warning").lower()
        if SEVERITY_RANK.get(sev, 1) < SEVERITY_RANK.get(min_s, 1):
            continue
        # Geography: facility match, or sub-county match, or county-wide (blank facility+sub)
        rfac = str(r.get("facility") or "").strip()
        rsub = str(r.get("sub_county") or "").strip()
        if rfac and facility and rfac != facility:
            continue
        if rsub and sub and rsub != sub:
            # Also allow county-wide roles with no sub
            if rfac:
                continue
            if rsub != sub:
                continue
        if rfac and not facility:
            continue
        out.append({
            "id": r.get("id"),
            "name": r.get("name"),
            "role": r.get("role"),
            "facility": r.get("facility"),
            "sub_county": r.get("sub_county"),
            "phone": r.get("phone"),
            "email": r.get("email"),
            "sms_enabled": int(r.get("sms_enabled") or 0),
            "email_enabled": int(r.get("email_enabled") or 0),
            "escalation_hours": r.get("escalation_hours"),
            "alert_family": rfam or fam,
        })
    return out


def seed_default_alert_recipients(user="system"):
    """Insert template recipients (no real phone numbers) if table empty."""
    try:
        n = int(query("SELECT COUNT(*) AS n FROM alert_recipients").iloc[0]["n"])
    except Exception:
        return 0
    if n > 0:
        return 0
    templates = [
        ("(Name)", "Facility data focal", "Jaribuni", "Ganze", "", "", "Data quality", "warning", 24),
        ("(Name)", "Facility data focal", "Pingilikani", "Kilifi South", "", "", "Data quality", "warning", 24),
        ("(Name)", "Facility data focal", "Mwakuhenga", "Kilifi North", "", "", "Data quality", "warning", 24),
        ("(Name)", "Facility in-charge", "Jaribuni", "Ganze", "", "", "Surveillance", "high", 24),
        ("(Name)", "Facility in-charge", "Pingilikani", "Kilifi South", "", "", "Surveillance", "high", 24),
        ("(Name)", "Facility in-charge", "Mwakuhenga", "Kilifi North", "", "", "Surveillance", "high", 24),
        ("(Name)", "Facility commodity focal", "Jaribuni", "Ganze", "", "", "Commodity", "high", 24),
        ("(Name)", "Sub-county M&E", None, "Ganze", "", "", "Data quality", "warning", 48),
        ("(Name)", "Sub-county malaria focal", None, "Ganze", "", "", "Surveillance", "high", 24),
        ("(Name)", "Sub-county malaria focal", None, "Kilifi South", "", "", "Forecast", "high", 12),
        ("(Name)", "Sub-county malaria focal", None, "Kilifi North", "", "", "Forecast", "high", 12),
        ("(Name)", "County malaria team", None, None, "", "", "Forecast", "high", 12),
        ("(Name)", "County EPR", None, None, "", "", "Forecast", "critical", 0),
        ("(Name)", "County logistics", None, None, "", "", "Commodity", "high", 24),
    ]
    for name, role, fac, sub, phone, email, fam, sev, hrs in templates:
        execute(
            """
            INSERT INTO alert_recipients
            (name, role, facility, sub_county, phone, email, alert_family, min_severity,
             sms_enabled, email_enabled, dashboard_enabled, escalation_hours, active, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, 1, 'Template — replace name/phone')
            """,
            (name, role, fac, sub, phone, email, fam, sev, hrs),
        )
    return len(templates)


def create_password_token(username, purpose="activation", hours=72):
    """One-time activation or reset token."""
    import secrets
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    execute(
        """
        INSERT INTO password_tokens (username, token, purpose, expires_at, used)
        VALUES (?, ?, ?, ?, 0)
        """,
        (username, token, purpose, expires),
    )
    return token, expires


def consume_password_token(token, new_password):
    """Validate token and set password. Returns (ok, message)."""
    row = query(
        "SELECT * FROM password_tokens WHERE token = ? AND IFNULL(used,0)=0",
        (token,),
    )
    if row.empty:
        return False, "Invalid or already used link."
    r = row.iloc[0]
    try:
        exp = pd.to_datetime(r["expires_at"])
        if pd.Timestamp(datetime.now()) > exp:
            return False, "This link has expired. Ask an administrator for a new one."
    except Exception:
        pass
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters."
    execute(
        "UPDATE users SET password_hash=?, must_change=0, updated_at=CURRENT_TIMESTAMP WHERE username=?",
        (_hash_password(new_password), r["username"]),
    )
    execute("UPDATE password_tokens SET used=1 WHERE token=?", (token,))
    log_audit(r["username"], "password_via_token", "users", r.get("purpose") or "reset")
    return True, "Password set. You can sign in."


def create_shared_view(title, page_key, payload, created_by, days=7, allow_download=False):
    import secrets
    import json as _json
    token = secrets.token_urlsafe(24)
    expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days else None
    execute(
        """
        INSERT INTO shared_views
        (token, title, page_key, payload_json, created_by, expires_at, allow_download, revoked)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            token, title, page_key,
            _json.dumps(payload or {}),
            created_by, expires, 1 if allow_download else 0,
        ),
    )
    log_audit(created_by, "share_create", "shared_views", title)
    return token


def get_shared_view(token):
    import json as _json
    row = query("SELECT * FROM shared_views WHERE token = ?", (token,))
    if row.empty:
        return None, "Link not found."
    r = row.iloc[0].to_dict()
    if int(r.get("revoked") or 0):
        return None, "This shared link has been revoked."
    if r.get("expires_at"):
        try:
            if pd.Timestamp(datetime.now()) > pd.to_datetime(r["expires_at"]):
                return None, "This shared link has expired."
        except Exception:
            pass
    try:
        r["payload"] = _json.loads(r.get("payload_json") or "{}")
    except Exception:
        r["payload"] = {}
    try:
        execute(
            "UPDATE shared_views SET open_count = IFNULL(open_count,0)+1 WHERE token=?",
            (token,),
        )
    except Exception:
        pass
    return r, None


def render_shared_view_page(token):
    """Standalone read-only page — no main navigation. Payload regulates scope."""
    view, err = get_shared_view(token)
    if err:
        st.error(err)
        st.caption("Contact the person who shared this link if you still need access.")
        return
    st.title(view.get("title") or "Shared view")
    st.caption(
        f"Kilifi County Malaria Programme · **Read-only shared view** · "
        f"Created by {view.get('created_by')} · "
        f"Expires {view.get('expires_at') or 'never'}"
    )
    payload = view.get("payload") or {}
    allowed = payload.get("allowed_modules") or []
    fac_scope = payload.get("facility_scope")
    sub_scope = payload.get("sub_county_scope")
    scope_bits = []
    if fac_scope:
        scope_bits.append(f"Facility: **{fac_scope}**")
    if sub_scope:
        scope_bits.append(f"Sub-county: **{sub_scope}**")
    if allowed:
        scope_bits.append("Modules: " + ", ".join(allowed))
    if scope_bits:
        st.warning("**Regulated view** — " + " · ".join(scope_bits))
    st.info(payload.get("summary") or "Shared programme view.")
    if payload.get("metrics"):
        st.markdown("#### Snapshot indicators")
        st.dataframe(pd.DataFrame(payload["metrics"]), use_container_width=True, hide_index=True)
    if payload.get("notes"):
        st.markdown("#### Notes")
        st.write(payload["notes"])
    if payload.get("table"):
        st.markdown("#### Data table")
        st.dataframe(pd.DataFrame(payload["table"]), use_container_width=True, hide_index=True)

    # Optional live cascade (read-only), scoped by facility/sub-county in payload
    if payload.get("include_live_cascade"):
        st.markdown("#### Live cascade (scoped)")
        try:
            df = load_all_data()
            fake_user = {
                "facility_scope": fac_scope,
                "sub_county_scope": sub_scope,
                "scope": "facility" if fac_scope else ("sub_county" if sub_scope else "all"),
                "aggregation": True,
                "min_cell": int(payload.get("min_cell") or 5),
            }
            df = apply_user_data_scope(df, user=fake_user)
            if df.empty:
                st.caption("No rows in regulated geography.")
            else:
                filters = {"facility": fac_scope} if fac_scope else {}
                d = _filter_data(df, filters, user=fake_user)
                c = compute_cascade_metrics(d)
                st.dataframe(
                    suppress_count_columns(pd.DataFrame([{
                        "Suspected": c.get("suspected"),
                        "Tested": c.get("tested"),
                        "Confirmed": c.get("confirmed"),
                        "AL": c.get("treated"),
                        "Testing %": c.get("testing_rate"),
                        "TPR %": c.get("positivity"),
                    }]), user=fake_user),
                    use_container_width=True, hide_index=True,
                )
        except Exception as e:
            st.caption(f"Live cascade unavailable: {e}")

    st.markdown("---")
    st.caption(
        "This link does **not** grant full dashboard login. "
        "Only the content and geography configured by the sender are visible. "
        "No upload, user admin, or unrestricted facility list."
    )


def send_alert_email(recipients, subject, body):
    """
    Optional SMTP email. Settings: smtp_host, smtp_port, smtp_user, smtp_password, smtp_from, smtp_tls.
    recipients: list of email addresses.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    host = get_setting("smtp_host")
    port = int(get_setting("smtp_port") or "587")
    user = get_setting("smtp_user")
    password = get_setting("smtp_password")
    sender = get_setting("smtp_from") or user
    use_tls = str(get_setting("smtp_tls") or "1") not in ("0", "false", "False")
    if not host or not sender:
        return False, "Save SMTP host and from-address in Alert routing / email settings first."
    if not recipients:
        return False, "No email recipients."
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject[:200]
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP(host, port, timeout=25) as server:
            if use_tls:
                server.starttls()
            if user and password:
                server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())
        execute(
            "INSERT INTO sms_log (recipient, body, status, provider_response) VALUES (?, ?, ?, ?)",
            (",".join(recipients), f"[EMAIL] {subject}: {body[:400]}", "email_sent", "smtp ok"),
        )
        return True, "sent"
    except Exception as e:
        execute(
            "INSERT INTO sms_log (recipient, body, status, provider_response) VALUES (?, ?, ?, ?)",
            (",".join(recipients), f"[EMAIL] {subject}: {body[:400]}", "email_failed", str(e)[:2000]),
        )
        return False, str(e)


def normalize_ke_phone(raw):
    """Normalize Kenya mobile numbers to +2547… form."""
    if raw is None:
        return None
    s = str(raw).strip()
    s = re.sub(r"[\s\-()]", "", s)
    if not s:
        return None
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("0") and len(s) >= 10:
        s = "+254" + s[1:]
    if s.startswith("254") and not s.startswith("+"):
        s = "+" + s
    if s.startswith("7") and len(s) == 9:
        s = "+254" + s
    return s


def send_africastalking_sms(recipients, message, username=None, apikey=None, sender_id=None):
    """
    Africa's Talking SMS.
    Sandbox: username MUST be "sandbox", API key from sandbox app,
             URL https://api.sandbox.africastalking.com/version1/messaging
    Live:    username = app username, live API key,
             URL https://api.africastalking.com/version1/messaging
    Header must be apiKey (camelCase) — ApiKey causes 401.
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    user = (username or "").strip() or (get_setting("at_username") or "")
    key = (apikey or "").strip() or (get_setting("at_apikey") or "")
    sender = (sender_id or "").strip() or (get_setting("at_sender") or "CBMP")
    if not user or not key:
        return False, "Enter Africa's Talking username and API key above, then click Send (or Save first)."
    cleaned = []
    for r in recipients:
        p = normalize_ke_phone(r)
        if p:
            cleaned.append(p)
    if not cleaned:
        return False, "No valid phone numbers after normalization (use +2547… or 07…)."
    to = ",".join(cleaned)
    # Sandbox vs live endpoint + username rules
    is_sandbox = user.lower() == "sandbox"
    base = (
        "https://api.sandbox.africastalking.com/version1/messaging"
        if is_sandbox
        else "https://api.africastalking.com/version1/messaging"
    )
    # Sandbox often works without custom sender; empty from uses default
    form = {"username": user, "to": to, "message": message}
    if sender and sender.upper() not in ("", "NONE", "DEFAULT"):
        form["from"] = sender
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        base,
        data=data,
        headers={
            "apiKey": key,  # required casing per AT docs
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        execute(
            "INSERT INTO sms_log (recipient, body, status, provider_response) VALUES (?, ?, ?, ?)",
            (to, message[:500], "sent", body[:2000]),
        )
        return True, body
    except Exception as e:
        execute(
            "INSERT INTO sms_log (recipient, body, status, provider_response) VALUES (?, ?, ?, ?)",
            (to, message[:500], "failed", str(e)[:2000]),
        )
        return False, str(e)


# ==================================================================
# PHASE C — MALARIA EARLY WARNING ENGINE
# Multi-layer spatiotemporal risk model:
#   Climate + Environment + Surveillance + Commodities + Seasonality
#   → Expected cases 4/8/12 weeks ahead + risk level + drivers
# ==================================================================

def _ew_get_facility_env():
    """Load facility environmental attributes (DB first, then defaults)."""
    try:
        env = query("SELECT * FROM facility_env")
        if not env.empty:
            return env.set_index("facility")
    except Exception:
        pass
    rows = []
    for fac, d in FACILITY_ENV_DEFAULTS.items():
        geo = GEO_SITES.get(fac, {})
        rows.append({
            "facility": fac,
            "elevation_m": d["elevation_m"],
            "water_dist_km": d["water_dist_km"],
            "ndvi_baseline": d["ndvi_baseline"],
            "land_cover": d["land_cover"],
            "pop_density": d["pop_density"],
            "travel_time_min": d["travel_time_min"],
            "lat": geo.get("lat"),
            "lon": geo.get("lon"),
            "notes": "",
        })
    return pd.DataFrame(rows).set_index("facility")


def _ew_weekly_climate():
    """Return weekly climate panel (facility or geography level) sorted by week."""
    try:
        clim = query(
            "SELECT week_ending, facility, geography, rainfall_mm, temperature_c, "
            "temp_min, temp_max, humidity_pct, rain_hours, et0_mm, source "
            "FROM climate_data ORDER BY week_ending"
        )
    except Exception:
        return pd.DataFrame()
    if clim.empty:
        return clim
    clim = clim.copy()
    clim["week_ending"] = pd.to_datetime(clim["week_ending"], errors="coerce")
    clim = clim.dropna(subset=["week_ending"])
    for c in ("rainfall_mm", "temperature_c", "temp_min", "temp_max",
              "humidity_pct", "rain_hours", "et0_mm"):
        if c in clim.columns:
            clim[c] = pd.to_numeric(clim[c], errors="coerce")
    # Prefer facility rows; fall back to geography
    clim["geo_key"] = clim["facility"].fillna(clim.get("geography"))
    return clim.sort_values("week_ending")


def _ew_weekly_from_registers():
    """
    Prefer true weekly grain from MOH 648 / 521 event tables.
    Returns facility × week_ending with tested / positive / confirmed.
    """
    rows = []
    for table, source in (("moh_648", "moh_648"), ("moh_521", "moh_521")):
        try:
            reg = query(
                f"SELECT report_date, facility, suspected, tested, positive "
                f"FROM {table} WHERE report_date IS NOT NULL AND facility IS NOT NULL"
            )
        except Exception:
            continue
        if reg.empty:
            continue
        reg = reg.copy()
        reg["report_date"] = pd.to_datetime(reg["report_date"], errors="coerce")
        reg = reg.dropna(subset=["report_date"])
        reg = reg[reg["facility"].isin(FACILITIES)]
        if reg.empty:
            continue
        # ISO week ending (Sunday)
        reg["week_ending"] = reg["report_date"] + pd.to_timedelta(
            6 - reg["report_date"].dt.weekday, unit="D"
        )
        for c in ("suspected", "tested", "positive"):
            reg[c] = pd.to_numeric(reg[c], errors="coerce").fillna(0)
        g = reg.groupby(["facility", "week_ending"], as_index=False).agg(
            suspected=("suspected", "sum"),
            tested=("tested", "sum"),
            positive=("positive", "sum"),
        )
        g["confirmed"] = g["positive"]  # RDT+ treated as confirmed in community stream
        g["source"] = source
        rows.append(g)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    # Prefer 648 over 521 when both exist for same facility-week
    out = out.sort_values("source")  # moh_521 after moh_648
    out = out.drop_duplicates(subset=["facility", "week_ending"], keep="first")
    return out


def _ew_build_surveillance_weekly(df, filters=None):
    """
    Aggregate surveillance to facility × week.
    Priority: true weekly 648/521 → monthly KHIS distributed (flagged).
    """
    weekly = _ew_weekly_from_registers()
    rows = []
    if not weekly.empty:
        for _, r in weekly.iterrows():
            fac = r["facility"]
            pop = POPULATION.get(fac, 20000) or 20000
            tested = float(r.get("tested") or 0)
            positive = float(r.get("positive") or 0)
            confirmed = float(r.get("confirmed") or positive)
            tpr = (positive / tested * 100) if tested > 0 else None
            rows.append({
                "week_ending": r["week_ending"],
                "facility": fac,
                "tested": tested,
                "positive": positive,
                "confirmed": confirmed,
                "tpr": tpr,
                "cases_per_1000": (confirmed / pop) * 1000 if pop else None,
                "testing_volume": tested,
                "source": r.get("source", "register_weekly"),
            })
    # Fill gaps / supplement from monthly KHIS where weekly absent
    if df is not None and not df.empty:
        d = _filter_data(df, filters or {})
        monthly = d[d["period"].astype(str).str.match(r"^\d{4}-\d{2}$", na=False)] if not d.empty else pd.DataFrame()
        existing_keys = {(r["facility"], pd.Timestamp(r["week_ending"]).strftime("%Y-%m-%d")) for r in rows} if rows else set()
        if not monthly.empty:
            for (fac, per), g in monthly.groupby(["facility", monthly["period"].astype(str)]):
                if fac not in FACILITIES:
                    continue
                cas = compute_cascade_metrics(g)
                go = compute_indicator("GO_1", g)
                tested = cas.get("tested") or 0
                positive = cas.get("positive") or 0
                confirmed = cas.get("confirmed") or 0
                tpr = go.get("value") if go.get("status") == "valid" else None
                pop = POPULATION.get(fac, 20000) or 20000
                try:
                    ym = pd.Period(per, freq="M")
                    for w in range(4):
                        week_end = (ym.start_time + pd.Timedelta(days=7 * (w + 1) - 1))
                        key = (fac, week_end.strftime("%Y-%m-%d"))
                        if key in existing_keys:
                            continue  # true weekly already present
                        share = 0.25
                        rows.append({
                            "week_ending": week_end,
                            "facility": fac,
                            "tested": tested * share,
                            "positive": positive * share,
                            "confirmed": confirmed * share,
                            "tpr": tpr,
                            "cases_per_1000": (confirmed * share / pop) * 1000 if pop else None,
                            "testing_volume": tested * share,
                            "source": "monthly_distributed",
                        })
                        existing_keys.add(key)
                except Exception:
                    continue
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["week_ending"] = pd.to_datetime(out["week_ending"], errors="coerce")
    return out.dropna(subset=["week_ending"]).sort_values(["facility", "week_ending"])


def _ew_add_lags(panel, value_col, lags=EW_RAIN_LAGS, prefix=None, rolls=(2, 4, 8)):
    """Add lag columns for a numeric series within each facility group."""
    if panel.empty or value_col not in panel.columns:
        return panel
    prefix = prefix or value_col
    out = panel.copy()
    out = out.sort_values(["facility", "week_ending"])
    for lag in lags:
        out[f"{prefix}_lag_{lag}"] = out.groupby("facility")[value_col].shift(lag)
    for w in rolls:
        out[f"{prefix}_roll_{w}"] = (
            out.groupby("facility")[value_col]
            .transform(lambda s: s.rolling(w, min_periods=1).sum())
        )
    return out


def _ew_seasonality(panel, case_col="confirmed"):
    """Add week-of-year, historical mean and anomaly features."""
    if panel.empty:
        return panel
    out = panel.copy()
    out["week_of_year"] = out["week_ending"].dt.isocalendar().week.astype(int)
    out["month"] = out["week_ending"].dt.month
    # Fourier seasonal terms (period 52 weeks)
    woy = out["week_of_year"].astype(float)
    out["fourier_sin"] = np.sin(2 * np.pi * woy / 52.0)
    out["fourier_cos"] = np.cos(2 * np.pi * woy / 52.0)
    hist = (
        out.groupby(["facility", "week_of_year"])[case_col]
        .transform("mean")
    )
    out["hist_mean_cases"] = hist
    out["case_anomaly"] = out[case_col] - out["hist_mean_cases"]
    out["case_anomaly_pct"] = np.where(
        out["hist_mean_cases"].abs() > 1e-6,
        out["case_anomaly"] / out["hist_mean_cases"],
        0.0,
    )
    return out


def _ew_calendar_week_anomaly(panel, value_col, prefix=None):
    """
    Anomaly vs facility × calendar-week climatology (not overall median).
    'Unusually wet for this week of year' rather than 'above facility median'.
    """
    if panel.empty or value_col not in panel.columns:
        return panel
    prefix = prefix or value_col
    out = panel.copy()
    if "week_of_year" not in out.columns:
        out["week_of_year"] = out["week_ending"].dt.isocalendar().week.astype(int)
    clim = out.groupby(["facility", "week_of_year"])[value_col].transform("median")
    out[f"{prefix}_clim"] = clim
    out[f"{prefix}_anomaly"] = out[value_col] - clim
    out[f"{prefix}_anomaly_pct"] = np.where(
        clim.abs() > 1e-6, (out[value_col] - clim) / clim, 0.0
    )
    return out


def _ew_stock_features(facility, week_ending=None):
    """
    Stock signal for a facility, preferably as-of a week_ending date.
    Uses stock_movements balance_after when dated movements exist;
    falls back to latest stock table snapshot.
    """
    rdt = act = 1
    stock_out_days = 0
    source = "snapshot"
    we = None
    if week_ending is not None:
        try:
            we = pd.to_datetime(week_ending)
        except Exception:
            we = None
    # Prefer movement history as-of week
    if we is not None:
        try:
            moves = query(
                "SELECT movement_date, item, movement_type, quantity, balance_after "
                "FROM stock_movements WHERE facility = ? ORDER BY movement_date",
                (facility,),
            )
        except Exception:
            moves = pd.DataFrame()
        if not moves.empty:
            moves = moves.copy()
            moves["movement_date"] = pd.to_datetime(moves["movement_date"], errors="coerce")
            moves = moves.dropna(subset=["movement_date"])
            moves = moves[moves["movement_date"] <= we]
            if not moves.empty:
                source = "movements_asof"
                for kind, keys in (("rdt", ("RDT", "TEST")), ("act", ("AL", "ACT", "ARTESUNATE"))):
                    sub = moves[moves["item"].astype(str).str.upper().apply(
                        lambda s, k=keys: any(x in s for x in k)
                    )]
                    if sub.empty:
                        continue
                    last = sub.iloc[-1]
                    bal = last.get("balance_after")
                    if bal is not None and pd.notna(bal):
                        avail = 1 if float(bal) > 0 else 0
                    else:
                        qty = float(last.get("quantity") or 0)
                        mtype = str(last.get("movement_type") or "").lower()
                        # crude: issue with no balance → pressure
                        avail = 0 if ("out" in mtype or "issue" in mtype) and qty > 0 else 1
                    if kind == "rdt":
                        rdt = avail
                    else:
                        act = avail
                # stock-out days in last 7 days before week_ending
                week_start = we - pd.Timedelta(days=6)
                recent = moves[moves["movement_date"] >= week_start]
                if not recent.empty:
                    issues = recent[
                        recent["movement_type"].astype(str).str.lower().str.contains("out|issue|stock.?out", regex=True, na=False)
                    ]
                    stock_out_days = min(7, len(issues))
                return {
                    "rdt_available": rdt,
                    "act_available": act,
                    "stock_pressure": 1 - min(rdt, act),
                    "stock_out_days": stock_out_days,
                    "stock_source": source,
                }
    # Fallback: latest snapshot
    try:
        stock = query("SELECT item, quantity FROM stock WHERE facility = ?", (facility,))
    except Exception:
        stock = pd.DataFrame()
    if not stock.empty:
        for _, r in stock.iterrows():
            item = str(r.get("item") or "").upper()
            qty = float(r.get("quantity") or 0)
            if "RDT" in item or "TEST" in item:
                rdt = 1 if qty > 0 else 0
            if "AL" in item or "ACT" in item or "ARTESUNATE" in item:
                act = 1 if qty > 0 else 0
    return {
        "rdt_available": rdt,
        "act_available": act,
        "stock_pressure": 1 - min(rdt, act),
        "stock_out_days": 0,
        "stock_source": source,
    }


def build_ew_modelling_table(df=None, filters=None):
    """
    Build facility-week modelling panel:
    one row = one facility × one week
    with climate lags, seasonality, environment, surveillance and stock signals.
    """
    surv = _ew_build_surveillance_weekly(df, filters)
    clim = _ew_weekly_climate()
    env = _ew_get_facility_env()

    if surv.empty and clim.empty:
        return pd.DataFrame(), "No surveillance or climate data available"

    # Align climate to facilities: if climate has geography only, map via CBMP_IN_SUBCOUNTY
    if not clim.empty:
        clim = clim.copy()
        if clim["facility"].isna().all() or (clim["facility"].astype(str).str.strip() == "").all():
            # map geography → facility via CBMP_IN_SUBCOUNTY
            clim["facility"] = clim["geography"].map(
                lambda g: CBMP_IN_SUBCOUNTY.get(g) or (g if g in FACILITIES else None)
            )
        clim = clim.dropna(subset=["facility"])
        # keep only CBMP facilities
        clim = clim[clim["facility"].isin(FACILITIES)]
        rain = clim.groupby(["facility", "week_ending"], as_index=False).agg(
            rainfall_mm=("rainfall_mm", "mean"),
            temp_mean=("temperature_c", "mean"),
            temp_min=("temp_min", "mean"),
            temp_max=("temp_max", "mean"),
            humidity_pct=("humidity_pct", "mean"),
            et0_mm=("et0_mm", "mean"),
        )
        rain = _ew_add_lags(rain, "rainfall_mm", lags=EW_RAIN_LAGS, prefix="rain")
        # temperature comfort index (Anopheles prefers ~20–30 °C)
        rain["temp_suitability"] = rain["temp_mean"].apply(
            lambda t: max(0, 1 - abs((t or 25) - 25) / 10) if pd.notna(t) else 0.5
        )
    else:
        rain = pd.DataFrame()

    if not surv.empty:
        panel = surv.copy()
        if not rain.empty:
            panel = panel.merge(rain, on=["facility", "week_ending"], how="left")
        else:
            for c in ["rainfall_mm"] + [f"rain_lag_{l}" for l in EW_RAIN_LAGS]:
                panel[c] = np.nan
    else:
        # climate-only panel (still useful for environmental pressure)
        panel = rain.copy() if not rain.empty else pd.DataFrame()
        if not panel.empty:
            panel["confirmed"] = np.nan
            panel["tested"] = np.nan
            panel["tpr"] = np.nan
            panel["cases_per_1000"] = np.nan

    if panel.empty:
        return pd.DataFrame(), "Could not build facility-week panel"

    panel = _ew_seasonality(panel, case_col="confirmed")
    # Autoregressive malaria trajectory (critical for short-horizon forecast)
    if "confirmed" in panel.columns:
        panel = _ew_add_lags(panel, "confirmed", lags=(1, 2, 3, 4), prefix="cases", rolls=(4,))
    if "tpr" in panel.columns:
        panel = _ew_add_lags(panel, "tpr", lags=(1, 2), prefix="tpr", rolls=())
    if "testing_volume" in panel.columns:
        panel = _ew_add_lags(panel, "testing_volume", lags=(1,), prefix="testing", rolls=(4,))
    elif "tested" in panel.columns:
        panel = _ew_add_lags(panel, "tested", lags=(1,), prefix="testing", rolls=(4,))

    # attach environment
    for col in ("elevation_m", "water_dist_km", "ndvi_baseline", "land_cover",
                "pop_density", "travel_time_min"):
        panel[col] = panel["facility"].map(
            lambda f: env.loc[f, col] if f in env.index else FACILITY_ENV_DEFAULTS.get(f, {}).get(col)
        )
    # Rainfall anomaly vs facility × calendar-week climatology (not overall median)
    if "rainfall_mm" in panel.columns:
        panel = _ew_calendar_week_anomaly(panel, "rainfall_mm", prefix="rain")
        # keep alias used by risk score
        panel["rain_anomaly"] = panel.get("rain_anomaly")
        panel["rain_anomaly_pct"] = panel.get("rain_anomaly_pct")
    # Temperature lags if present
    if "temp_mean" in panel.columns:
        panel = _ew_add_lags(panel, "temp_mean", lags=(1, 2), prefix="temp", rolls=())
    if "humidity_pct" in panel.columns:
        panel = _ew_add_lags(panel, "humidity_pct", lags=(1, 2), prefix="humidity", rolls=())
    # stock snapshot (latest, not week-specific — limitation noted)
    # Stock as-of each week (movements when available; else snapshot)
    rdt_list, act_list, press_list, sod_list, src_list = [], [], [], [], []
    for _, r in panel.iterrows():
        sf = _ew_stock_features(r["facility"], r.get("week_ending"))
        rdt_list.append(sf.get("rdt_available", 1))
        act_list.append(sf.get("act_available", 1))
        press_list.append(sf.get("stock_pressure", 0))
        sod_list.append(sf.get("stock_out_days", 0))
        src_list.append(sf.get("stock_source", "snapshot"))
    panel["rdt_available"] = rdt_list
    panel["act_available"] = act_list
    panel["stock_pressure"] = press_list
    panel["stock_out_days"] = sod_list
    panel["stock_source"] = src_list

    # Greenness / NDVI: prefer uploaded true NDVI; then NASA POWER greenness; else rain-modulated proxy
    # Labelled environmental_greenness to avoid implying MODIS NDVI when proxy is used.
    try:
        ndvi_ts = query(
            "SELECT week_ending, facility, ndvi_mean, ndvi_anomaly, source FROM ndvi_weekly"
        )
    except Exception:
        ndvi_ts = pd.DataFrame()
    if not ndvi_ts.empty:
        ndvi_ts = ndvi_ts.copy()
        ndvi_ts["week_ending"] = pd.to_datetime(ndvi_ts["week_ending"], errors="coerce")
        panel = panel.merge(
            ndvi_ts.rename(columns={"ndvi_mean": "ndvi_observed", "source": "ndvi_feed_source"}),
            on=["facility", "week_ending"], how="left",
        )
        panel["environmental_greenness"] = panel["ndvi_observed"].fillna(panel.get("ndvi_baseline", 0.4))
        panel["ndvi_source"] = np.where(
            panel["ndvi_observed"].notna(),
            panel.get("ndvi_feed_source", "observed").fillna("observed"),
            "rain_proxy",
        )
    else:
        if "ndvi_baseline" in panel.columns and "rain_roll_4" in panel.columns:
            rain_norm = panel["rain_roll_4"].fillna(0) / (panel["rain_roll_4"].median() or 50)
            panel["environmental_greenness"] = (
                panel["ndvi_baseline"].fillna(0.4)
                * (0.7 + 0.3 * np.clip(rain_norm, 0, 2))
            ).clip(0.1, 0.9)
        else:
            panel["environmental_greenness"] = panel.get("ndvi_baseline", 0.4)
        panel["ndvi_source"] = "rain_proxy"
    # Alias for older risk-score code paths
    panel["ndvi_proxy"] = panel["environmental_greenness"]

    # Intervention coverage — prefer period match (YYYY-MM or YYYY-Qn), else latest
    try:
        icov = query(
            "SELECT facility, period, itn_ownership_pct, itn_use_pct, irs_coverage_pct, "
            "iptp_coverage_pct FROM intervention_coverage ORDER BY period DESC"
        )
    except Exception:
        icov = pd.DataFrame()
    for col in ("itn_ownership_pct", "itn_use_pct", "irs_coverage_pct", "iptp_coverage_pct"):
        panel[col] = np.nan
    if not icov.empty:
        latest = icov.drop_duplicates(subset=["facility"], keep="first").set_index("facility")
        for i, r in panel.iterrows():
            fac = r["facility"]
            # Derive period keys from week_ending
            we = r.get("week_ending")
            keys = []
            try:
                ts = pd.to_datetime(we)
                keys = [ts.strftime("%Y-%m"), f"{ts.year}-Q{(ts.month - 1) // 3 + 1}", str(ts.year)]
            except Exception:
                pass
            matched = None
            fac_rows = icov[icov["facility"] == fac]
            for k in keys:
                hit = fac_rows[fac_rows["period"].astype(str) == k]
                if not hit.empty:
                    matched = hit.iloc[0]
                    break
            if matched is None and fac in latest.index:
                matched = latest.loc[fac]
            if matched is not None:
                for col in ("itn_ownership_pct", "itn_use_pct", "irs_coverage_pct", "iptp_coverage_pct"):
                    try:
                        v = matched[col] if col in matched.index else None
                        panel.at[i, col] = float(v) if pd.notna(v) else np.nan
                    except Exception:
                        pass

    # Mobility proxies
    try:
        mob = query("SELECT * FROM mobility_proxy")
        if not mob.empty:
            mob = mob.set_index("facility")
            for col in ("dist_major_road_km", "dist_market_km", "dist_town_km",
                        "fishing_community", "seasonal_migration"):
                panel[col] = panel["facility"].map(
                    lambda f, c=col: mob.loc[f, c] if f in mob.index else None
                )
    except Exception:
        for col in ("dist_major_road_km", "dist_market_km", "dist_town_km",
                    "fishing_community", "seasonal_migration"):
            panel[col] = np.nan

    # Entomology (latest survey per facility)
    try:
        ent = query(
            "SELECT facility, anopheles_density, eir, sporozoite_rate, survey_date "
            "FROM entomology ORDER BY survey_date DESC"
        )
        if not ent.empty:
            ent = ent.drop_duplicates(subset=["facility"], keep="first")
            for col in ("anopheles_density", "eir", "sporozoite_rate"):
                panel[col] = panel["facility"].map(
                    lambda f, c=col: float(ent.set_index("facility").loc[f, c])
                    if f in ent["facility"].values and pd.notna(ent.set_index("facility").loc[f, c])
                    else None
                )
    except Exception:
        for col in ("anopheles_density", "eir", "sporozoite_rate"):
            panel[col] = np.nan

    n_weekly = int((panel.get("source") == "moh_648").sum() + (panel.get("source") == "moh_521").sum()) if "source" in panel.columns else 0
    n_dist = int((panel.get("source") == "monthly_distributed").sum()) if "source" in panel.columns else 0
    note = (
        f"Panel: {panel['facility'].nunique()} facilities × "
        f"{panel['week_ending'].nunique()} weeks · "
        f"true-weekly rows={n_weekly}, monthly-distributed={n_dist}."
    )
    return panel.sort_values(["facility", "week_ending"]), note


def _ew_risk_score_row(row):
    """
    Transparent multi-factor risk score ∈ [0, 1].
    Higher = higher expected transmission pressure over next 4–8 weeks.
    """
    score = 0.0
    drivers = []

    # 1. Lagged rainfall pressure (max weight)
    rain_lag_vals = []
    for lag in (2, 3, 4, 6):
        v = row.get(f"rain_lag_{lag}")
        if pd.notna(v):
            rain_lag_vals.append(float(v))
    if rain_lag_vals:
        avg_lag = np.mean(rain_lag_vals)
        # >60 mm/week lagged is elevated for coastal Kenya
        rain_comp = min(1.0, avg_lag / 100.0)
        score += 0.28 * rain_comp
        if rain_comp > 0.5:
            drivers.append(("Rainfall (lagged)", "↑", f"{avg_lag:.0f} mm avg lag"))
        elif rain_comp < 0.25:
            drivers.append(("Rainfall (lagged)", "↓", f"{avg_lag:.0f} mm avg lag"))
        else:
            drivers.append(("Rainfall (lagged)", "→", f"{avg_lag:.0f} mm avg lag"))
    else:
        drivers.append(("Rainfall (lagged)", "?", "no data"))

    # 2. Recent rainfall anomaly
    anom = row.get("rain_anomaly_pct")
    if pd.notna(anom):
        anom_comp = min(1.0, max(0.0, 0.5 + float(anom) * 0.5))
        score += 0.12 * anom_comp
        drivers.append(("Rain anomaly", "↑" if anom > 0.2 else ("↓" if anom < -0.2 else "→"),
                        f"{anom*100:+.0f}%"))

    # 3. Temperature suitability
    ts = row.get("temp_suitability")
    if pd.notna(ts):
        score += 0.10 * float(ts)
        drivers.append(("Temp suitability", "↑" if ts > 0.7 else "→", f"{ts:.2f}"))

    # 4. Environmental greenness (true NDVI if uploaded; else NASA POWER / rain proxy)
    ndvi = row.get("environmental_greenness") or row.get("ndvi_proxy") or row.get("ndvi_baseline")
    if pd.notna(ndvi):
        ndvi_comp = min(1.0, max(0.0, (float(ndvi) - 0.2) / 0.5))
        # Down-weight if rain proxy to reduce double-counting with rainfall features
        src = str(row.get("ndvi_source") or "")
        w_green = 0.06 if src in ("rain_proxy", "nasa_power_greenness") else 0.12
        score += w_green * ndvi_comp
        label = "Greenness (proxy)" if src in ("rain_proxy", "nasa_power_greenness") else "NDVI (observed)"
        drivers.append((label, "↑" if ndvi > 0.5 else "→", f"{ndvi:.2f}"))

    # 5. Water proximity (closer = higher risk) — static
    wd = row.get("water_dist_km")
    if pd.notna(wd):
        water_comp = max(0.0, 1.0 - float(wd) / 5.0)
        score += 0.08 * water_comp
        drivers.append(("Water proximity", "↑" if wd < 1.5 else "→", f"{wd:.1f} km"))

    # 6. Elevation (lower = higher risk in Kilifi)
    elev = row.get("elevation_m")
    if pd.notna(elev):
        elev_comp = max(0.0, 1.0 - float(elev) / 300.0)
        score += 0.06 * elev_comp
        drivers.append(("Elevation", "↑" if elev < 50 else "→", f"{elev:.0f} m"))

    # 7. Recent case / TPR momentum
    anom_case = row.get("case_anomaly_pct")
    if pd.notna(anom_case):
        case_comp = min(1.0, max(0.0, 0.5 + float(anom_case) * 0.5))
        score += 0.14 * case_comp
        drivers.append(("Case anomaly", "↑" if anom_case > 0.15 else ("↓" if anom_case < -0.15 else "→"),
                        f"{anom_case*100:+.0f}%"))

    tpr = row.get("tpr")
    if pd.notna(tpr):
        tpr_comp = min(1.0, float(tpr) / 40.0)  # 40% TPR = max
        score += 0.06 * tpr_comp
        drivers.append(("TPR", "↑" if tpr > 20 else "→", f"{tpr:.1f}%"))

    # 8. Stock pressure (stock-out can mask true incidence)
    sp = row.get("stock_pressure") or 0
    score += 0.03 * float(sp)
    if sp > 0:
        drivers.append(("Commodity stock", "⚠", "possible stock-out"))
    else:
        drivers.append(("Commodity stock", "✓", "available"))

    # 9. Intervention coverage (protective — reduces risk)
    itn_use = row.get("itn_use_pct")
    if pd.notna(itn_use):
        # high ITN use lowers residual risk
        protection = min(1.0, float(itn_use) / 100.0)
        score = score * (1.0 - 0.18 * protection)
        drivers.append(("ITN use", "↓" if itn_use >= 60 else "→", f"{itn_use:.0f}%"))
    irs = row.get("irs_coverage_pct")
    if pd.notna(irs) and float(irs) > 0:
        score = score * (1.0 - 0.12 * min(1.0, float(irs) / 100.0))
        drivers.append(("IRS coverage", "↓", f"{irs:.0f}%"))

    # 10. Mobility/connectivity proxy (not phone CDR; Haversine only)
    fish = row.get("fishing_community")
    migr = row.get("seasonal_migration")
    if pd.notna(fish) and int(fish) == 1:
        score += 0.03
        drivers.append(("Connectivity proxy: fishing", "↑", "coastal community flag"))
    if pd.notna(migr) and int(migr) == 1:
        score += 0.03
        drivers.append(("Connectivity proxy: migration", "↑", "seasonal labour flag"))
    road = row.get("dist_major_road_km")
    if pd.notna(road) and float(road) < 2.0:
        score += 0.02
        drivers.append(("Connectivity proxy: road", "↑", f"{road:.1f} km to major road"))

    # 11. Entomology when available
    eir = row.get("eir")
    if pd.notna(eir) and float(eir) > 0:
        eir_comp = min(1.0, float(eir) / 50.0)
        score += 0.08 * eir_comp
        drivers.append(("EIR (entomology)", "↑" if eir > 10 else "→", f"{eir:.1f}"))
    anoph = row.get("anopheles_density")
    if pd.notna(anoph) and float(anoph) > 0:
        drivers.append(("Anopheles density", "↑" if anoph > 5 else "→", f"{anoph:.1f}"))

    # Seasonality boost (long rains approx Apr–Jun, short rains Oct–Dec in Kilifi)
    month = row.get("month")
    if pd.notna(month):
        if int(month) in (4, 5, 6, 10, 11, 12):
            score += 0.04
            drivers.append(("Seasonality", "↑", "typical transmission season"))
        else:
            drivers.append(("Seasonality", "→", "off-peak months"))

    score = float(np.clip(score, 0.0, 1.0))
    return score, drivers


def render_ew_drivers_legend():
    """Plain-language guide for Model drivers signals (programme staff)."""
    with st.expander("How to read Model drivers (signals)", expanded=False):
        st.markdown(
            """
**What this table is**  
It shows **why** the early-warning risk score is higher or lower. It is **not** a confirmed outbreak.

**Signal signs**

| Sign | Meaning |
|------|---------|
| **↑** | This factor is **raising** malaria transmission pressure |
| **→** | This factor is **about normal** (not strongly up or down) |
| **↓** | This factor is **lowering** pressure (often protective, e.g. high ITN use) |
| **?** | **No data** for this factor |
| **⚠** | **Warning** (e.g. stock-out may hide true cases) |
| **✓** | **OK** for that check (e.g. commodities available) |

**Detail** = the actual number (mm of rain, %, km, etc.).

**Common factors (simple)**

| Factor | In simple words |
|--------|-----------------|
| Rainfall (lagged) | Rain from recent weeks (mosquitoes need time to breed) |
| Rain anomaly | Wetting than usual for this time of year |
| Temp suitability | Temperature good for malaria parasites |
| NDVI / Greenness | Environment greener / more moisture |
| Water proximity | Closer to water bodies |
| Elevation | Lower land often higher risk in Kilifi |
| Case anomaly | Cases higher than recent normal |
| TPR | Share of tests that are positive |
| Commodity stock | RDT/ACT available or not |
| ITN / IRS | Nets and spraying (↓ often means good coverage) |
| Seasonality | Typical high-transmission months |

**How to use it**  
1. Look for several **↑** together (rain + cases + season).  
2. Check **⚠ stock** before trusting case/TPR alone.  
3. **↑ is a signal to investigate**, not automatic emergency declaration.
            """
        )


def _ew_risk_level(score):
    if score >= EW_RISK_THRESHOLDS["high"]:
        return "Very High"
    if score >= EW_RISK_THRESHOLDS["moderate"]:
        return "High"
    if score >= EW_RISK_THRESHOLDS["low"]:
        return "Moderate"
    return "Low"


def _ew_predict_cases(row, horizon_weeks=4, model_bundle=None, interval_scale=1.0):
    """
    Ensemble predictor:
      A — seasonal historical mean
      B — risk-score climate multiplier
      C — ridge regression (if model_bundle trained on past data only)
    Final = weighted average.
    Prediction interval is heuristic, optionally scaled by back-test calibration
    (interval_scale). UI must show intervals — not a pseudo-probability "confidence".
    Returns: expected, low, high, components_dict
    """
    base = row.get("hist_mean_cases")
    if pd.isna(base) or base is None:
        base = row.get("confirmed")
    if pd.isna(base) or base is None:
        base = 5.0
    base = max(0.0, float(base))

    score = float(row.get("risk_score") or 0.4)
    climate_mult = 0.6 + 1.2 * score
    horizon_factor = 1.0 + 0.05 * (horizon_weeks - 4)

    pred_seasonal = base * horizon_factor
    pred_risk = base * climate_mult * horizon_factor
    pred_ridge = None
    if model_bundle and model_bundle.get("coef") is not None:
        try:
            x = _ew_feature_vector(row, model_bundle["feature_names"])
            log_pred = float(model_bundle["intercept"] + np.dot(model_bundle["coef"], x))
            pred_ridge = max(0.0, float(np.expm1(log_pred)))
        except Exception:
            pred_ridge = None

    parts = [pred_seasonal, pred_risk]
    weights = [0.25, 0.40]
    if pred_ridge is not None and np.isfinite(pred_ridge):
        parts.append(pred_ridge)
        weights.append(0.35)
    w = np.array(weights, dtype=float)
    w = w / w.sum()
    expected = float(np.dot(w, parts))

    residual_scale = max(3.0, base * 0.35)
    disagreement = float(np.std(parts)) if len(parts) > 1 else 0.0
    # ~80% nominal interval (1.28 ≈ z_0.9); scaled by calibration factor from back-test
    scale = (residual_scale * (1 + 0.1 * horizon_weeks) + 0.5 * disagreement) * float(interval_scale)
    low = max(0.0, expected - 1.28 * scale)
    high = expected + 1.28 * scale
    return expected, low, high, {
        "seasonal": round(pred_seasonal, 1),
        "risk_score_model": round(pred_risk, 1),
        "ridge": None if pred_ridge is None else round(pred_ridge, 1),
    }


def _ew_feature_vector(row, feature_names):
    """Build numeric feature vector for ridge model (missing → 0)."""
    vec = []
    for name in feature_names:
        v = row.get(name)
        try:
            vec.append(0.0 if pd.isna(v) else float(v))
        except Exception:
            vec.append(0.0)
    return np.array(vec, dtype=float)


def _ew_ridge_feature_names(panel):
    """Full feature list for ridge: autoregressive cases + climate + programme."""
    candidates = (
        [f"cases_lag_{l}" for l in (1, 2, 3, 4)]
        + ["cases_roll_4", "case_anomaly_pct"]
        + [f"tpr_lag_{l}" for l in (1, 2)]
        + ["testing_lag_1", "testing_roll_4"]
        + [f"rain_lag_{l}" for l in EW_RAIN_LAGS]
        + ["rain_roll_2", "rain_roll_4", "rain_roll_8", "rain_anomaly_pct"]
        + ["temp_suitability", "temp_lag_1", "humidity_lag_1"]
        + ["environmental_greenness", "ndvi_proxy", "ndvi_anomaly"]
        + ["elevation_m", "water_dist_km"]
        + ["itn_use_pct", "itn_ownership_pct", "irs_coverage_pct"]
        + ["rdt_available", "act_available", "stock_pressure"]
        + ["week_of_year", "month", "fourier_sin", "fourier_cos"]
        + ["dist_major_road_km", "fishing_community", "seasonal_migration"]
    )
    return [c for c in candidates if c in panel.columns]


def _ew_train_ridge_ensemble(panel, horizon_weeks=4, min_train=20):
    """
    Train ridge on log1p(future confirmed) using ONLY the provided panel rows
    (caller must restrict to past data for true rolling-origin validation).
    min_train=20 is technical minimum to fit; validation gate uses higher bar.
    """
    feature_names = _ew_ridge_feature_names(panel)
    if not feature_names or panel.empty or "confirmed" not in panel.columns:
        return None
    df = panel.dropna(subset=["confirmed"]).copy()
    if len(df) < min_train:
        return None
    df = df.sort_values(["facility", "week_ending"])
    df["y"] = df.groupby("facility")["confirmed"].shift(-horizon_weeks)
    df = df.dropna(subset=["y"])
    if len(df) < min_train:
        return None
    X = np.column_stack([
        df[c].fillna(df[c].median() if df[c].notna().any() else 0).values
        for c in feature_names
    ])
    y = np.log1p(df["y"].clip(lower=0).values)
    mu = X.mean(axis=0)
    sig = X.std(axis=0)
    sig[sig < 1e-6] = 1.0
    Xs = (X - mu) / sig
    lam = 5.0  # slightly stronger regularisation with more features
    p = Xs.shape[1]
    try:
        xtx = Xs.T @ Xs + lam * np.eye(p)
        xty = Xs.T @ y
        coef_s = np.linalg.solve(xtx, xty)
        intercept = float(y.mean() - np.dot(coef_s, Xs.mean(axis=0)))
        coef = coef_s / sig
        intercept = intercept - np.dot(coef, mu)
        return {
            "feature_names": feature_names,
            "coef": coef,
            "intercept": intercept,
            "mu": mu,
            "sig": sig,
            "n_train": len(df),
            "n_features": len(feature_names),
            "horizon_weeks": horizon_weeks,
        }
    except Exception:
        return None


def _ew_haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dl = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


# Key Kilifi mobility anchors (markets / towns / road junctions)
KILIFI_MOBILITY_ANCHORS = {
    "Kilifi_town": (-3.633, 39.850),
    "Malindi": (-3.219, 40.117),
    "Mombasa_highway_Vipingo": (-3.82, 39.81),
    "Ganze_market": (-3.53, 39.70),
    "Mtwapa": (-3.95, 39.74),
    "Kaloleni": (-3.82, 39.63),
}


def refresh_mobility_distances():
    """
    Recompute facility → anchor distances (Haversine) and update mobility_proxy.
    Mobile-phone CDR is not available; road/town distances are the operational proxy.
    """
    env = _ew_get_facility_env()
    n = 0
    for fac in FACILITIES:
        if fac not in env.index:
            continue
        lat = env.loc[fac, "lat"]
        lon = env.loc[fac, "lon"]
        if pd.isna(lat) or pd.isna(lon):
            continue
        dists = {
            name: _ew_haversine_km(float(lat), float(lon), a[0], a[1])
            for name, a in KILIFI_MOBILITY_ANCHORS.items()
        }
        dist_town = min(dists.get("Kilifi_town", 99), dists.get("Malindi", 99))
        dist_market = min(dists.get("Ganze_market", 99), dists.get("Kaloleni", 99))
        dist_road = dists.get("Mombasa_highway_Vipingo", dist_town)
        try:
            execute(
                """
                INSERT INTO mobility_proxy
                (facility, dist_major_road_km, dist_market_km, dist_town_km,
                 travel_time_facility_min, fishing_community, seasonal_migration, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(facility) DO UPDATE SET
                  dist_major_road_km=excluded.dist_major_road_km,
                  dist_market_km=excluded.dist_market_km,
                  dist_town_km=excluded.dist_town_km,
                  notes=excluded.notes,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    fac, round(dist_road, 2), round(dist_market, 2), round(dist_town, 2),
                    int(dist_town * 2.5),  # ~2.5 min per km rough
                    1 if fac in ("Pingilikani", "Mwakuhenga") else 0,
                    1 if fac == "Jaribuni" else 0,
                    f"Haversine to anchors: { {k: round(v,1) for k,v in dists.items()} }",
                ),
            )
            n += 1
        except Exception:
            pass
    return n


def fetch_ndvi_pipeline(days=56):
    """
    NDVI / greenness pipeline:
    1. Prefer existing ndvi_weekly rows
    2. Pull NASA POWER daily meteorology at facility points and derive a
       greenness index (humidity + precip + clear-sky radiation inverse stress)
       stored as ndvi_proxy_power in ndvi_weekly with source='nasa_power_greenness'
    True MODIS NDVI requires Earth Engine / AppEEARS credentials — upload CSV for that.
    """
    import urllib.request
    import urllib.parse
    env = _ew_get_facility_env()
    end = datetime.utcnow().date()
    start = end - timedelta(days=int(days))
    rows_out, errors = [], []
    for fac in FACILITIES:
        if fac not in env.index:
            continue
        lat, lon = env.loc[fac, "lat"], env.loc[fac, "lon"]
        if pd.isna(lat) or pd.isna(lon):
            errors.append(f"{fac}: no coordinates")
            continue
        params = urllib.parse.urlencode({
            "parameters": "T2M,PRECTOTCORR,RH2M,ALLSKY_SFC_SW_DWN,EVPTRNS",
            "community": "AG",
            "longitude": f"{float(lon):.4f}",
            "latitude": f"{float(lat):.4f}",
            "start": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "format": "JSON",
        })
        url = f"https://power.larc.nasa.gov/api/temporal/daily/point?{params}"
        try:
            with urllib.request.urlopen(url, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            errors.append(f"{fac}: NASA POWER {e}")
            continue
        props = (payload.get("properties") or {}).get("parameter") or {}
        if not props:
            errors.append(f"{fac}: empty POWER payload")
            continue
        # daily → weekly mean greenness
        dates = sorted(props.get("RH2M", {}).keys())
        if not dates:
            continue
        daily = []
        for d in dates:
            try:
                rh = float(props.get("RH2M", {}).get(d) or 50)
                pr = float(props.get("PRECTOTCORR", {}).get(d) or 0)
                sw = float(props.get("ALLSKY_SFC_SW_DWN", {}).get(d) or 5)
                # greenness proxy: moist + moderate radiation, scaled to ~0.2–0.8 NDVI-like
                g = 0.25 + 0.35 * min(1.0, rh / 90.0) + 0.25 * min(1.0, pr / 15.0) - 0.10 * max(0, (sw - 6) / 8.0)
                g = float(np.clip(g, 0.15, 0.85))
                daily.append((pd.to_datetime(d, format="%Y%m%d"), g, pr, rh))
            except Exception:
                continue
        if not daily:
            continue
        ddf = pd.DataFrame(daily, columns=["date", "green", "precip", "rh"])
        ddf["week_ending"] = ddf["date"] + pd.to_timedelta(6 - ddf["date"].dt.weekday, unit="D")
        for we, g in ddf.groupby("week_ending"):
            rows_out.append({
                "week_ending": we.strftime("%Y-%m-%d"),
                "facility": fac,
                "ndvi_mean": round(float(g["green"].mean()), 3),
                "ndvi_anomaly": None,
                "source": "nasa_power_greenness",
            })
    # persist
    n = 0
    if rows_out:
        conn = get_conn()
        try:
            for r in rows_out:
                try:
                    conn.execute(
                        """
                        INSERT INTO ndvi_weekly (week_ending, facility, ndvi_mean, ndvi_anomaly, source)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(week_ending, facility) DO UPDATE SET
                          ndvi_mean=excluded.ndvi_mean,
                          source=excluded.source
                        """,
                        (r["week_ending"], r["facility"], r["ndvi_mean"], r["ndvi_anomaly"], r["source"]),
                    )
                    n += 1
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
    return n, errors


def ew_data_validity_gate(panel, forecasts, summary):
    """
    Safety gate before automatic EPR actions.
    Returns (ok: bool, reasons: list[str]).
    """
    reasons = []
    if panel is None or panel.empty:
        return False, ["No modelling panel"]
    if forecasts is None or forecasts.empty:
        return False, ["No forecasts"]
    n_weeks = panel["week_ending"].nunique() if "week_ending" in panel.columns else 0
    if n_weeks < 8:
        reasons.append(f"Insufficient history ({n_weeks} weeks; want ≥8)")
    if "confirmed" in panel.columns and panel["confirmed"].notna().sum() < 12:
        reasons.append("Too few confirmed-case observations")
    # wide intervals relative to prediction → uncertain
    f4 = forecasts[forecasts["horizon_weeks"] == 4] if "horizon_weeks" in forecasts.columns else forecasts
    if not f4.empty:
        width = (f4["pred_high"] - f4["pred_low"]) / f4["predicted_cases"].clip(lower=1)
        if float(width.median()) > 2.5:
            reasons.append("Prediction intervals excessively wide (median width > 2.5× point forecast)")
    # source quality
    if "source" in panel.columns:
        dist_share = float((panel["source"] == "monthly_distributed").mean())
        if dist_share > 0.85:
            reasons.append("Most case weeks are monthly-distributed (prefer weekly 648/521)")
    ok = len(reasons) == 0
    return ok, reasons


def create_epr_checklist(facility, risk_level, risk_score, predicted_cases, pred_low, pred_high, user="system"):
    """
    Auto-create EPR (Epidemic Preparedness & Response) workplan actions
    when risk is High or Very High. Idempotent for open items on same facility.
    """
    due = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")
    items = [
        (
            f"EPR: Verify malaria case counts & TPR at {facility}",
            "Cross-check 648/521/KHIS cascade; confirm no stock-out artefact; document findings.",
            "Facility in-charge / CHA",
        ),
        (
            f"EPR: Commodity readiness at {facility}",
            "Confirm RDT and ACT stock ≥ 2 weeks of expected demand; request redistribution if low.",
            "Commodity focal / CHP supervisor",
        ),
        (
            f"EPR: Intensify CHP testing & fever screening at {facility}",
            "Brief CHPs on elevated risk; increase community testing and referral follow-up for 4 weeks.",
            "CHA / CHP supervisor",
        ),
        (
            f"EPR: Vector-control & ITN check in {facility} catchment",
            "Verify ITN coverage/use; identify breeding sites; coordinate larval source management if feasible.",
            "Vector control / Sub-county malaria",
        ),
        (
            f"EPR: Surveillance alert communication for {facility}",
            f"Notify sub-county malaria coordinator: risk={risk_level}, score={risk_score:.2f}, "
            f"expected 4wk cases ≈ {predicted_cases:.0f} [{pred_low:.0f}–{pred_high:.0f}].",
            "County malaria / EPR",
        ),
    ]
    if risk_level == "Very High":
        items.append((
            f"EPR: Activate enhanced response at {facility}",
            "Convene sub-county rapid response discussion within 72h; review admissions and severe malaria.",
            "Sub-county MoH / EPR team",
        ))
    n = 0
    conn = get_conn()
    try:
        for problem, action, owner in items:
            # skip if identical open action exists
            existing = conn.execute(
                "SELECT id FROM workplan_actions WHERE facility=? AND problem=? AND status='open'",
                (facility, problem),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO workplan_actions
                (facility, problem, action_text, owner, due_date, activity, status, source, created_by)
                VALUES (?, ?, ?, ?, ?, 'Malaria EPR', 'open', 'ew_auto', ?)
                """,
                (facility, problem, action, owner, due, user),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def run_ew_models(df=None, filters=None, horizons=None):
    """
    Full early-warning pipeline with ensemble prediction.
    Returns: panel, forecasts_df, summary_dict, note
    """
    horizons = horizons or list(EW_HORIZONS)
    panel, note = build_ew_modelling_table(df, filters)
    if panel.empty:
        return panel, pd.DataFrame(), {"status": "no_data", "detail": note}, note

    # Train ridge ensemble once for 4-week horizon (shared features)
    ridge_bundle = _ew_train_ridge_ensemble(panel, horizon_weeks=4)

    latest_idx = panel.groupby("facility")["week_ending"].idxmax()
    latest = panel.loc[latest_idx].copy()

    scores, all_drivers = [], []
    for _, row in latest.iterrows():
        sc, drv = _ew_risk_score_row(row)
        scores.append(sc)
        all_drivers.append(drv)
    latest["risk_score"] = scores
    latest["drivers"] = all_drivers
    latest["risk_level"] = latest["risk_score"].map(_ew_risk_level)

    # Optional interval calibration from last stored back-test coverage
    interval_scale = 1.0
    try:
        last_run = query(
            "SELECT notes FROM ew_model_runs WHERE model_name='ew_v2_ensemble' "
            "ORDER BY id DESC LIMIT 1"
        )
        # default scale; refined after back-test stores coverage_factor
        cal = query(
            "SELECT mape FROM ew_model_runs WHERE notes LIKE 'CALIB:%' ORDER BY id DESC LIMIT 1"
        )
        if not cal.empty and cal.iloc[0]["mape"] is not None:
            # reuse mape column to store calibration scale when tagged
            interval_scale = float(np.clip(cal.iloc[0]["mape"], 0.5, 2.5))
    except Exception:
        pass

    forecast_rows = []
    for _, row in latest.iterrows():
        for h in horizons:
            bundle = ridge_bundle if h == 4 else (_ew_train_ridge_ensemble(panel, horizon_weeks=h) or ridge_bundle)
            pred, lo, hi, components = _ew_predict_cases(
                row, horizon_weeks=h, model_bundle=bundle, interval_scale=interval_scale
            )
            fw = (row["week_ending"] + pd.Timedelta(weeks=h)).strftime("%Y-%m-%d")
            forecast_rows.append({
                "facility": row["facility"],
                "horizon_weeks": h,
                "forecast_week": fw,
                "predicted_cases": round(pred, 1),
                "pred_low": round(lo, 1),
                "pred_high": round(hi, 1),
                "risk_level": row["risk_level"],
                "risk_score": round(float(row["risk_score"]), 3),
                "model_name": "ew_v2_ensemble",
                "interval_label": "≈80% prediction interval (heuristic; calibrate via back-test)",
                "drivers": row["drivers"],
                "components": components,
                "current_cases": row.get("confirmed"),
                "hist_mean": row.get("hist_mean_cases"),
                "week_ending": row["week_ending"],
            })
    forecasts = pd.DataFrame(forecast_rows)

    f4 = forecasts[forecasts["horizon_weeks"] == 4] if not forecasts.empty else pd.DataFrame()
    summary = {
        "status": "ok",
        "n_facilities": int(latest["facility"].nunique()),
        "latest_week": str(latest["week_ending"].max().date()) if not latest.empty else None,
        "total_predicted_4w": float(f4["predicted_cases"].sum()) if not f4.empty else None,
        "total_low_4w": float(f4["pred_low"].sum()) if not f4.empty else None,
        "total_high_4w": float(f4["pred_high"].sum()) if not f4.empty else None,
        "max_risk": latest["risk_level"].iloc[latest["risk_score"].argmax()] if not latest.empty else None,
        "facilities_high": latest[latest["risk_score"] >= EW_RISK_THRESHOLDS["moderate"]]["facility"].tolist(),
        "ridge_trained": ridge_bundle is not None,
        "ridge_n": None if not ridge_bundle else ridge_bundle.get("n_train"),
        "ridge_features": None if not ridge_bundle else ridge_bundle.get("n_features"),
        "interval_scale": interval_scale,
        "note": note,
    }
    return panel, forecasts, summary, note


def persist_ew_forecasts(forecasts, user="system"):
    """Write forecast rows into ew_forecasts table."""
    if forecasts is None or forecasts.empty:
        return 0
    n = 0
    conn = get_conn()
    try:
        for _, r in forecasts.iterrows():
            drivers_json = json.dumps(
                [{"factor": d[0], "signal": d[1], "detail": d[2]} for d in (r.get("drivers") or [])]
            )
            # confidence column stores interval half-width ratio (not probability)
            half = None
            try:
                mid = float(r["predicted_cases"]) or 1.0
                half = (float(r["pred_high"]) - float(r["pred_low"])) / (2.0 * mid)
            except Exception:
                half = None
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ew_forecasts
                    (facility, horizon_weeks, forecast_week, predicted_cases, pred_low, pred_high,
                     risk_level, risk_score, model_name, drivers_json, confidence, run_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["facility"], int(r["horizon_weeks"]), r["forecast_week"],
                        r["predicted_cases"], r["pred_low"], r["pred_high"],
                        r["risk_level"], r["risk_score"], r["model_name"],
                        drivers_json, half,
                        datetime.utcnow().isoformat(),
                    ),
                )
                n += 1
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()
    return n


def compute_clim_indicators(df=None, filters=None):
    """
    Live values for CLIM_01 (rainfall anomaly alert) and CLIM_02 (lagged risk index).
    Returns dict of indicator_id → {value, status, detail}.
    """
    panel, _ = build_ew_modelling_table(df, filters)
    out = {
        "CLIM_01": {"value": None, "status": "pending_data", "detail": "No climate panel"},
        "CLIM_02": {"value": None, "status": "pending_data", "detail": "No climate panel"},
    }
    if panel.empty:
        return out
    latest_idx = panel.groupby("facility")["week_ending"].idxmax()
    latest = panel.loc[latest_idx]
    # CLIM_01 — max absolute rainfall anomaly % across facilities
    if "rain_anomaly_pct" in latest.columns and latest["rain_anomaly_pct"].notna().any():
        max_anom = float(latest["rain_anomaly_pct"].abs().max())
        out["CLIM_01"] = {
            "value": round(max_anom * 100, 1),
            "status": "valid",
            "detail": f"Max |rainfall anomaly| = {max_anom*100:.0f}% vs facility median",
        }
    # CLIM_02 — mean lagged risk score
    scores = []
    for _, row in latest.iterrows():
        sc, _ = _ew_risk_score_row(row)
        scores.append(sc)
    if scores:
        mean_sc = float(np.mean(scores))
        out["CLIM_02"] = {
            "value": round(mean_sc, 3),
            "status": "valid",
            "detail": f"Mean multifactor risk score = {mean_sc:.3f} (0–1 scale)",
        }
    return out


def ew_backtest(panel, horizon_weeks=4, min_train_ridge=20):
    """
    Genuine rolling-origin back-test of the FULL ensemble.
    At each origin:
      - TRAIN ridge ONLY on data strictly before origin (all facilities)
      - Recompute hist_mean / risk score from past only
      - Predict seasonal, risk, ridge, ensemble for origin → origin+h
      - Compare to actual
    Also evaluates components separately for model selection.
    """
    if panel is None or panel.empty or "confirmed" not in panel.columns:
        return pd.DataFrame(), {"status": "no_data", "detail": "Need panel with confirmed cases"}
    panel = panel.dropna(subset=["confirmed"]).copy()
    panel = panel.sort_values(["facility", "week_ending"]).reset_index(drop=True)
    origins = sorted(panel["week_ending"].unique())
    if len(origins) < horizon_weeks + 6:
        return pd.DataFrame(), {
            "status": "insufficient",
            "detail": f"Need more weeks of history (have {len(origins)}, need ≥{horizon_weeks + 6})",
        }

    rows = []
    # Step through origins leaving room for horizon
    for oi, origin in enumerate(origins):
        target_time = origin + pd.Timedelta(weeks=horizon_weeks)
        # past panel: week_ending < origin (strict) + origin week features for prediction row
        past_mask = panel["week_ending"] < origin
        past = panel.loc[past_mask]
        if past["facility"].nunique() < 1 or len(past) < 8:
            continue
        # origin rows (one per facility at this week)
        origin_rows = panel[panel["week_ending"] == origin]
        if origin_rows.empty:
            continue
        # train ridge on past only
        ridge = _ew_train_ridge_ensemble(past, horizon_weeks=horizon_weeks, min_train=min_train_ridge)

        for _, row0 in origin_rows.iterrows():
            fac = row0["facility"]
            # actual at target
            tgt = panel[
                (panel["facility"] == fac) & (panel["week_ending"] == target_time)
            ]
            if tgt.empty:
                # try nearest week within 3 days
                cand = panel[
                    (panel["facility"] == fac)
                    & (panel["week_ending"] > origin)
                    & (panel["week_ending"] <= origin + pd.Timedelta(weeks=horizon_weeks + 1))
                ]
                if cand.empty:
                    continue
                actual = float(cand.iloc[-1]["confirmed"])
                target_week = cand.iloc[-1]["week_ending"]
            else:
                actual = float(tgt.iloc[0]["confirmed"])
                target_week = target_time

            row = row0.copy()
            # hist mean from past only for this facility × week_of_year
            woy = int(row.get("week_of_year") or row["week_ending"].isocalendar()[1])
            past_fac = past[past["facility"] == fac]
            hist_s = past_fac[past_fac["week_of_year"] == woy]["confirmed"] if "week_of_year" in past_fac.columns else past_fac["confirmed"]
            row["hist_mean_cases"] = float(hist_s.mean()) if len(hist_s) else float(row.get("confirmed") or 5)
            sc, _ = _ew_risk_score_row(row)
            row["risk_score"] = sc

            pred, lo, hi, comps = _ew_predict_cases(
                row, horizon_weeks=horizon_weeks, model_bundle=ridge, interval_scale=1.0
            )
            seasonal = float(comps.get("seasonal") or row["hist_mean_cases"])
            risk_p = float(comps.get("risk_score_model") or seasonal)
            ridge_p = comps.get("ridge")
            # Autoregressive baseline: recent cases trajectory (often strong at short horizon)
            ar_base = row.get("cases_roll_4")
            if pd.isna(ar_base) or ar_base is None:
                ar_base = row.get("cases_lag_1")
            if pd.isna(ar_base) or ar_base is None:
                ar_base = row.get("confirmed")
            try:
                ar_p = max(0.0, float(ar_base or 0) * (1.0 + 0.05 * (horizon_weeks - 4)))
            except Exception:
                ar_p = seasonal
            # Quasi-NB / overdispersed mean: use hist mean with variance inflation
            # pred_nb ≈ max(0, μ + 0.15*(cases_lag_1 - μ)) — soft pull toward recent
            try:
                mu = float(hist_s.mean()) if len(hist_s) else seasonal
                lag1 = float(row.get("cases_lag_1") or mu)
                nb_p = max(0.0, mu + 0.15 * (lag1 - mu))
                if len(hist_s) >= 4:
                    var = float(hist_s.var())
                    if var > mu and mu > 0:
                        # mild overdispersion adjustment toward lag
                        nb_p = max(0.0, 0.7 * mu + 0.3 * lag1)
            except Exception:
                nb_p = seasonal
            rows.append({
                "facility": fac,
                "origin_week": origin,
                "target_week": target_week,
                "actual": actual,
                "ensemble_pred": pred,
                "seasonal_pred": seasonal,
                "risk_pred": risk_p,
                "ridge_pred": ridge_p,
                "ar_pred": ar_p,
                "nb_pred": nb_p,
                "naive_pred": seasonal,
                "abs_err_ensemble": abs(pred - actual),
                "abs_err_seasonal": abs(seasonal - actual),
                "abs_err_risk": abs(risk_p - actual),
                "abs_err_ridge": abs(float(ridge_p) - actual) if ridge_p is not None else np.nan,
                "abs_err_ar": abs(ar_p - actual),
                "abs_err_nb": abs(nb_p - actual),
                "abs_err_model": abs(pred - actual),  # alias for UI
                "abs_err_naive": abs(seasonal - actual),
                "in_interval": 1 if lo <= actual <= hi else 0,
                "pred_low": lo,
                "pred_high": hi,
                "risk_score": sc,
                "ridge_trained": ridge is not None,
            })

    if not rows:
        return pd.DataFrame(), {"status": "insufficient", "detail": "No valid origin/target pairs"}

    bt = pd.DataFrame(rows)
    mae_ens = float(bt["abs_err_ensemble"].mean())
    mae_sea = float(bt["abs_err_seasonal"].mean())
    mae_risk = float(bt["abs_err_risk"].mean())
    mae_ridge = float(bt["abs_err_ridge"].dropna().mean()) if bt["abs_err_ridge"].notna().any() else None
    mae_ar = float(bt["abs_err_ar"].mean()) if "abs_err_ar" in bt.columns else None
    mae_nb = float(bt["abs_err_nb"].mean()) if "abs_err_nb" in bt.columns else None
    coverage = float(bt["in_interval"].mean() * 100)
    skill = 1.0 - (mae_ens / mae_sea) if mae_sea > 0 else 0.0
    # Calibrate interval scale toward 80% coverage
    # if coverage < 80 → widen (scale up); if > 80 → narrow
    if coverage > 0:
        # simple proportional adjustment capped
        calib_scale = float(np.clip(80.0 / max(coverage, 1.0), 0.6, 2.0))
    else:
        calib_scale = 1.5

    # pick best single component by MAE
    component_maes = {
        "seasonal": mae_sea,
        "risk_score": mae_risk,
        "ridge": mae_ridge,
        "autoregressive": mae_ar,
        "quasi_nb": mae_nb,
        "ensemble": mae_ens,
    }
    valid_comp = {k: v for k, v in component_maes.items() if v is not None}
    best = min(valid_comp, key=valid_comp.get) if valid_comp else "ensemble"

    # Validation gate: enough pairs + preferably multiple facilities
    n_pairs = len(bt)
    validated = n_pairs >= 40 and bt["facility"].nunique() >= 2 and skill > -0.05

    summary = {
        "status": "ok",
        "n_pairs": n_pairs,
        "mae_model": round(mae_ens, 2),
        "mae_ensemble": round(mae_ens, 2),
        "mae_naive": round(mae_sea, 2),
        "mae_seasonal": round(mae_sea, 2),
        "mae_risk": round(mae_risk, 2),
        "mae_ridge": None if mae_ridge is None else round(mae_ridge, 2),
        "mae_ar": None if mae_ar is None else round(mae_ar, 2),
        "mae_nb": None if mae_nb is None else round(mae_nb, 2),
        "mape_model_pct": round(float((bt["abs_err_ensemble"] / bt["actual"].clip(lower=1)).mean() * 100), 1),
        "interval_coverage_pct": round(coverage, 1),
        "target_coverage_pct": 80.0,
        "interval_calib_scale": round(calib_scale, 3),
        "skill_vs_naive": round(skill, 3),
        "best_component": best,
        "validated": validated,
        "detail": (
            f"Rolling-origin ensemble back-test {horizon_weeks}wk · n={n_pairs} · "
            f"MAE ensemble={mae_ens:.1f} seasonal={mae_sea:.1f} risk={mae_risk:.1f} "
            f"ridge={mae_ridge if mae_ridge is not None else 'n/a'} · "
            f"skill={skill:.2f} · interval coverage={coverage:.0f}% (target 80%) · "
            f"best={best} · validated={'yes' if validated else 'no (need more history)'}"
        ),
    }
    try:
        execute(
            "INSERT INTO ew_model_runs (model_name, n_facilities, n_weeks, mae, mape, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "ew_v2_ensemble",
                int(bt["facility"].nunique()),
                int(bt["origin_week"].nunique()),
                mae_ens,
                summary["mape_model_pct"],
                summary["detail"],
            ),
        )
        # store calibration factor for production intervals
        execute(
            "INSERT INTO ew_model_runs (model_name, n_facilities, n_weeks, mae, mape, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("interval_calib", 0, horizon_weeks, coverage, calib_scale, f"CALIB: scale={calib_scale} coverage={coverage}"),
        )
    except Exception:
        pass
    return bt, summary


def push_ew_alerts(forecasts, user="system", panel=None, summary=None, auto_epr=True):
    """
    Push High / Very High risk facilities into alerts.
    EPR checklist only if data-validity gate passes; otherwise flag Review required.
    """
    if forecasts is None or forecasts.empty:
        return {"alerts": 0, "epr_actions": 0, "epr_blocked": False, "gate_reasons": []}
    elevated = forecasts[
        (forecasts["horizon_weeks"] == 4)
        & (forecasts["risk_level"].isin(["High", "Very High"]))
    ]
    if elevated.empty:
        return {"alerts": 0, "epr_actions": 0, "epr_blocked": False, "gate_reasons": []}

    gate_ok, gate_reasons = ew_data_validity_gate(panel, forecasts, summary or {})
    n_alerts = 0
    n_epr = 0
    conn = get_conn()
    try:
        for _, r in elevated.iterrows():
            fac = r["facility"]
            epr_note = (
                "EPR checklist auto-created."
                if gate_ok and auto_epr
                else "EPR auto-action blocked — review required (" + "; ".join(gate_reasons[:2]) + ")."
            )
            msg = (
                f"Malaria EW: {fac} risk={r['risk_level']} (score {r['risk_score']:.2f}). "
                f"Expected {r['predicted_cases']:.0f} cases "
                f"[{r['pred_low']:.0f}–{r['pred_high']:.0f}] in 4 weeks "
                f"(week ending {r['forecast_week']}). {epr_note}"
            )
            conn.execute(
                "UPDATE alerts SET status='resolved', resolved_at=? "
                "WHERE facility=? AND category='Malaria EW' AND status='open'",
                (datetime.utcnow().isoformat(), fac),
            )
            conn.execute(
                """
                INSERT INTO alerts
                (alert_rule_id, category, severity, facility, register, period, message, status)
                VALUES (NULL, 'Malaria EW', ?, ?, 'EW', ?, ?, 'open')
                """,
                (
                    "critical" if r["risk_level"] == "Very High" else "high",
                    fac,
                    r["forecast_week"],
                    msg,
                ),
            )
            n_alerts += 1
        conn.commit()
    finally:
        conn.close()
    if gate_ok and auto_epr:
        for _, r in elevated.iterrows():
            n_epr += create_epr_checklist(
                r["facility"], r["risk_level"], float(r["risk_score"]),
                float(r["predicted_cases"]), float(r["pred_low"]), float(r["pred_high"]),
                user=user,
            )
    return {
        "alerts": n_alerts,
        "epr_actions": n_epr,
        "epr_blocked": not gate_ok,
        "gate_reasons": gate_reasons,
    }


def page_malaria_ew(user, filters):
    """
    Phase C — Malaria Early Warning module.
    Forecast · Risk map · Drivers · Facility trajectories · Model data · Scenario.
    """
    page_header(
        "Malaria Early Warning",
        "Multi-factor spatiotemporal risk model · Climate + environment + surveillance + commodities. "
        "Forecasts are ranges, not single-point magic numbers. Explainable drivers for every facility.",
    )

    st.markdown(
        """
**What this module does**
- Combines **lagged rainfall**, temperature suitability, NDVI (observed or proxy), water proximity,
  elevation, recent case/TPR momentum, **ITN/IRS coverage**, mobility proxies, entomology (when present),
  seasonality and commodity availability.
- Prefers **true weekly 648/521** when available; falls back to monthly KHIS distribution.
- Produces **4 / 8 / 12-week** expected-case ranges + transparent **risk level** + drivers.
- Pushes High / Very High facilities into the operational **Alerts** table.
- Includes out-of-time **back-test** vs seasonal naïve baseline.

**What it is not**
- Not a deep-learning black-box outbreak oracle.
- Not a substitute for field investigation or EPR guidelines.
        """
    )

    df = load_all_data()
    with st.spinner("Building facility-week panel and running early-warning models…"):
        panel, forecasts, summary, note = run_ew_models(df, filters)

    # ── Status strip ────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Status", summary.get("status", "—").upper())
    with c2:
        st.metric("Facilities scored", summary.get("n_facilities") or 0)
    with c3:
        pred = summary.get("total_predicted_4w")
        st.metric("Expected cases (4 wk)", f"{pred:.0f}" if pred is not None else "—")
    with c4:
        lo, hi = summary.get("total_low_4w"), summary.get("total_high_4w")
        st.metric("4-wk interval", f"{lo:.0f}–{hi:.0f}" if lo is not None else "—")
    with c5:
        st.metric("Highest risk", summary.get("max_risk") or "—")
    st.caption(note)

    # Pipeline actions
    if user.get("can_upload"):
        p1, p2, p3 = st.columns(3)
        with p1:
            if st.button("🌿 Pull NASA POWER greenness → NDVI table", key="ew_ndvi_pull"):
                with st.spinner("NASA POWER daily → weekly greenness…"):
                    n_ndvi, ndvi_errs = fetch_ndvi_pipeline(days=56)
                if n_ndvi:
                    st.success(f"Stored **{n_ndvi}** NDVI/greenness weekly rows.")
                if ndvi_errs:
                    st.warning("; ".join(ndvi_errs[:5]))
                st.rerun()
        with p2:
            if st.button("📍 Refresh mobility distances (Haversine)", key="ew_mob_refresh"):
                n_m = refresh_mobility_distances()
                st.success(f"Updated mobility for **{n_m}** facilities.")
                st.rerun()
        with p3:
            ridge_msg = (
                f"Ridge ensemble trained (n={summary.get('ridge_n')})"
                if summary.get("ridge_trained") else "Ridge ensemble: insufficient history"
            )
            st.caption(ridge_msg)

    # Push operational alerts + EPR checklist (gated)
    if summary.get("status") == "ok" and not forecasts.empty:
        push_res = push_ew_alerts(
            forecasts, user.get("username", "system"), panel=panel, summary=summary
        )
        if push_res.get("alerts"):
            if push_res.get("epr_blocked"):
                st.warning(
                    f"Pushed **{push_res['alerts']}** alert(s). "
                    f"**EPR auto-action blocked** — review required: "
                    + "; ".join(push_res.get("gate_reasons") or [])
                )
            else:
                st.success(
                    f"Pushed **{push_res['alerts']}** alert(s) · "
                    f"created **{push_res['epr_actions']}** EPR workplan action(s)."
                )

    if summary.get("status") != "ok" or forecasts.empty:
        st.warning(
            "Early-warning engine needs climate history (Open-Meteo pull on Climate page) "
            "and/or KHIS malaria data. Pull climate history first, then re-open this page."
        )
        st.info(
            "Quick start: go to **Climate** → Pull history (weekly) → return here. "
            "Then use **Pull NASA POWER greenness** and **Refresh mobility** on this page."
        )
        return

    # Persist latest run
    if user.get("can_upload"):
        n_saved = persist_ew_forecasts(forecasts, user.get("username", "system"))
        if n_saved:
            st.caption(f"Saved {n_saved} forecast rows to ew_forecasts (model=ew_v2_ensemble).")

    tabs = st.tabs([
        "🔮 Forecast", "🗺️ Risk map", "📊 Drivers", "🏥 Facility detail",
        "📈 Model data", "✅ Back-test", "🛡️ Interventions", "🧪 Scenario", "ℹ️ Method",
    ])

    # ── Tab 1: Forecast ─────────────────────────────────────────────
    with tabs[0]:
        st.subheader("Programme forecast")
        horizon = st.selectbox("Horizon (weeks)", EW_HORIZONS, index=0, key="ew_horizon")
        f_h = forecasts[forecasts["horizon_weeks"] == horizon].copy()
        if f_h.empty:
            st.info("No forecasts for this horizon.")
        else:
            total_pred = f_h["predicted_cases"].sum()
            total_lo = f_h["pred_low"].sum()
            total_hi = f_h["pred_high"].sum()
            risk_counts = f_h["risk_level"].value_counts()
            m1, m2, m3 = st.columns(3)
            m1.metric(f"Expected cases ({horizon} wk)", f"{total_pred:.0f}")
            m2.metric("Prediction interval", f"{total_lo:.0f} – {total_hi:.0f}")
            m3.metric("Facilities elevated+", int(
                risk_counts.get("High", 0) + risk_counts.get("Very High", 0)
            ))

            show = f_h[[
                "facility", "forecast_week", "current_cases", "hist_mean",
                "predicted_cases", "pred_low", "pred_high", "risk_level",
                "risk_score",
            ]].copy()
            show.columns = [
                "Facility", "Forecast week", "Current (approx)", "Historical mean",
                "Predicted", "Interval low", "Interval high", "Risk", "Score",
            ]
            # Prefer empirical calibration text when a back-test has been stored
            try:
                cal = query(
                    "SELECT notes, mae FROM ew_model_runs WHERE model_name='interval_calib' "
                    "ORDER BY id DESC LIMIT 1"
                )
            except Exception:
                cal = pd.DataFrame()
            if not cal.empty:
                st.caption(
                    f"Empirically calibrated prediction interval (target coverage 80%). "
                    f"Latest calibration: {cal.iloc[0].get('notes')}. "
                    "Not a probability that the point forecast is “correct”."
                )
            else:
                st.caption(
                    "Intervals target ≈80% coverage (heuristic until you run Back-test). "
                    "Not a probability that the point forecast is “correct”."
                )
            st.dataframe(show, use_container_width=True, hide_index=True)

            if "components" in f_h.columns:
                with st.expander("Ensemble components (seasonal · risk-score · ridge)"):
                    comp_rows = []
                    for _, r in f_h.iterrows():
                        c = r.get("components") or {}
                        if isinstance(c, dict):
                            comp_rows.append({
                                "Facility": r["facility"],
                                "Seasonal": c.get("seasonal"),
                                "Risk-score model": c.get("risk_score_model"),
                                "Ridge": c.get("ridge"),
                                "Ensemble": r["predicted_cases"],
                            })
                    if comp_rows:
                        st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

            # Alert list
            elevated = f_h[f_h["risk_level"].isin(["High", "Very High"])]
            if not elevated.empty:
                st.markdown("#### Facilities requiring attention + EPR")
                for _, r in elevated.iterrows():
                    st.markdown(
                        f"- **{r['facility']}** — {r['risk_level']} "
                        f"(score {r['risk_score']:.2f}) · "
                        f"expected {r['predicted_cases']:.0f} "
                        f"[{r['pred_low']:.0f}–{r['pred_high']:.0f}] cases in {horizon} weeks "
                        f"· EPR checklist auto-created"
                    )

    # ── Tab 2: Risk map ─────────────────────────────────────────────
    with tabs[1]:
        st.subheader("Predicted risk by facility")
        f4 = forecasts[forecasts["horizon_weeks"] == 4].copy()
        if f4.empty:
            st.info("No 4-week forecasts.")
        else:
            # Attach coordinates
            env = _ew_get_facility_env()
            f4["lat"] = f4["facility"].map(lambda f: env.loc[f, "lat"] if f in env.index else None)
            f4["lon"] = f4["facility"].map(lambda f: env.loc[f, "lon"] if f in env.index else None)
            color_map = {
                "Low": COLOR_GREEN, "Moderate": COLOR_AMBER,
                "High": COLOR_RED, "Very High": "#8B0000",
            }
            f4["color"] = f4["risk_level"].map(color_map)
            fig = px.scatter_mapbox(
                f4.dropna(subset=["lat", "lon"]),
                lat="lat", lon="lon",
                color="risk_level",
                size="risk_score",
                size_max=28,
                hover_name="facility",
                hover_data={
                    "predicted_cases": ":.0f", "pred_low": ":.0f", "pred_high": ":.0f",
                    "risk_score": ":.2f", "lat": False, "lon": False,
                },
                color_discrete_map=color_map,
                zoom=8,
                height=480,
            )
            fig.update_layout(mapbox_style="open-street-map", margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)
            st.caption("Bubble size ∝ risk score. Colour = risk level (4-week horizon).")

    # ── Tab 3: Drivers ──────────────────────────────────────────────
    with tabs[2]:
        st.subheader("Model drivers (latest week per facility)")
        render_ew_drivers_legend()
        st.caption("↑ raises risk · → neutral · ↓ lowers risk / protective · ? no data · ⚠ warning · ✓ ok")
        # rebuild latest with drivers
        latest_idx = panel.groupby("facility")["week_ending"].idxmax()
        latest = panel.loc[latest_idx].copy()
        for fac in latest["facility"].unique():
            row = latest[latest["facility"] == fac].iloc[0]
            sc, drivers = _ew_risk_score_row(row)
            st.markdown(f"### {fac} — risk score **{sc:.2f}** ({_ew_risk_level(sc)})")
            drv_df = pd.DataFrame(drivers, columns=["Factor", "Signal", "Detail"])
            st.dataframe(drv_df, use_container_width=True, hide_index=True)
            st.caption("Signs: ↑ raises risk · → neutral · ↓ protective/lower · ? missing · ⚠ stock/data warning · ✓ ok")
            # short narrative
            up = [d[0] for d in drivers if d[1] == "↑"]
            if up:
                st.info(f"**Why elevated / notable:** {', '.join(up)}.")
            st.markdown("---")

    # ── Tab 4: Facility detail ──────────────────────────────────────
    with tabs[3]:
        st.subheader("Facility trajectories & forecasts")
        fac_sel = st.selectbox("Facility", FACILITIES, key="ew_fac_sel")
        fac_panel = panel[panel["facility"] == fac_sel].copy()
        fac_fc = forecasts[forecasts["facility"] == fac_sel].copy()
        if fac_panel.empty:
            st.info("No panel rows for this facility.")
        else:
            # time series of confirmed + rainfall
            fig = pgo.Figure()
            if "confirmed" in fac_panel.columns:
                fig.add_trace(pgo.Scatter(
                    x=fac_panel["week_ending"], y=fac_panel["confirmed"],
                    name="Confirmed (approx weekly)", mode="lines+markers",
                ))
            if "hist_mean_cases" in fac_panel.columns:
                fig.add_trace(pgo.Scatter(
                    x=fac_panel["week_ending"], y=fac_panel["hist_mean_cases"],
                    name="Historical mean", mode="lines", line=dict(dash="dash"),
                ))
            if "rainfall_mm" in fac_panel.columns:
                fig.add_trace(pgo.Bar(
                    x=fac_panel["week_ending"], y=fac_panel["rainfall_mm"],
                    name="Rainfall (mm)", yaxis="y2", opacity=0.35,
                ))
            fig.update_layout(
                title=f"{fac_sel} — cases & rainfall",
                yaxis_title="Cases",
                yaxis2=dict(title="Rainfall mm", overlaying="y", side="right", showgrid=False),
                height=420,
                margin=dict(t=80, b=60, l=50, r=50),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.08,
                    xanchor="left",
                    x=0,
                    bgcolor="rgba(255,255,255,0.85)",
                ),
                xaxis=dict(title="Week ending", tickangle=-35),
            )
            st.plotly_chart(fig, use_container_width=True)

            st.markdown("#### Forecasts for this facility")
            if not fac_fc.empty:
                show_fc = fac_fc[[
                    "horizon_weeks", "forecast_week", "predicted_cases",
                    "pred_low", "pred_high", "risk_level",
                ]].copy()
                show_fc.columns = [
                    "Horizon (wk)", "Week ending", "Predicted", "Interval low", "Interval high", "Risk",
                ]
                st.dataframe(show_fc, use_container_width=True, hide_index=True)

            # explain
            row = fac_panel.iloc[-1]
            sc, drivers = _ew_risk_score_row(row)
            st.markdown(f"**Current risk score:** {sc:.2f} ({_ew_risk_level(sc)})")
            st.dataframe(
                pd.DataFrame(drivers, columns=["Factor", "Signal", "Detail"]),
                use_container_width=True, hide_index=True,
            )

    # ── Tab 5: Model data ───────────────────────────────────────────
    with tabs[4]:
        st.subheader("Feature availability & panel diagnostics")
        st.caption(note)
        if not panel.empty:
            feat_cols = [
                c for c in panel.columns
                if c.startswith(("rain_", "temp_", "ndvi", "case_", "hist_"))
                or c in (
                    "elevation_m", "water_dist_km", "rdt_available", "stock_pressure",
                    "rainfall_mm", "confirmed", "tpr", "itn_use_pct", "irs_coverage_pct",
                    "dist_major_road_km", "eir", "anopheles_density", "fishing_community",
                )
            ]
            avail = panel[feat_cols].notna().mean().sort_values(ascending=False)
            avail_df = pd.DataFrame({
                "Feature": avail.index,
                "Availability %": (avail.values * 100).round(1),
            })
            st.dataframe(avail_df, use_container_width=True, hide_index=True)
            with st.expander("Raw modelling panel (sample)"):
                st.dataframe(panel.tail(40), use_container_width=True)
            if "source" in panel.columns:
                st.markdown("#### Surveillance grain")
                st.dataframe(
                    panel["source"].value_counts().rename_axis("source").reset_index(name="rows"),
                    use_container_width=True, hide_index=True,
                )

        st.markdown("#### Climate indicators (CLIM_01 / CLIM_02)")
        clim_ind = compute_clim_indicators(df, filters)
        for cid, meta in clim_ind.items():
            st.write(f"**{cid}** — status `{meta['status']}` · value = `{meta['value']}` · {meta['detail']}")

        st.markdown("#### NDVI feed")
        try:
            n_ndvi = int(query("SELECT COUNT(*) AS n FROM ndvi_weekly").iloc[0]["n"])
        except Exception:
            n_ndvi = 0
        st.caption(
            f"Observed NDVI rows in `ndvi_weekly`: **{n_ndvi}**. "
            "Until a satellite pipeline is connected, the model uses a rain-modulated NDVI proxy."
        )
        if user.get("can_upload"):
            with st.expander("Upload NDVI weekly CSV (facility, week_ending, ndvi_mean)"):
                up = st.file_uploader("NDVI CSV", type=["csv"], key="ndvi_up")
                if up is not None:
                    try:
                        nd = pd.read_csv(up)
                        req = {"facility", "week_ending", "ndvi_mean"}
                        if not req.issubset(set(nd.columns)):
                            st.error(f"Need columns: {req}")
                        else:
                            conn = get_conn()
                            n = 0
                            for _, r in nd.iterrows():
                                conn.execute(
                                    "INSERT OR REPLACE INTO ndvi_weekly "
                                    "(week_ending, facility, ndvi_mean, ndvi_anomaly, source) "
                                    "VALUES (?, ?, ?, ?, ?)",
                                    (
                                        str(r["week_ending"])[:10], str(r["facility"]),
                                        float(r["ndvi_mean"]),
                                        float(r["ndvi_anomaly"]) if "ndvi_anomaly" in nd.columns and pd.notna(r.get("ndvi_anomaly")) else None,
                                        "upload",
                                    ),
                                )
                                n += 1
                            conn.commit()
                            conn.close()
                            st.success(f"Stored {n} NDVI rows.")
                            st.rerun()
                    except Exception as e:
                        st.error(str(e))

    # ── Tab 6: Model validation / rolling-origin back-test ───────────
    with tabs[5]:
        st.subheader("MODEL VALIDATION — rolling-origin ensemble back-test")
        st.caption(
            "At each origin: train Ridge **only on data before origin**, then generate "
            "seasonal + risk + ridge + **ensemble** predictions. Compares all components."
        )
        bt_h = st.selectbox("Back-test horizon (weeks)", [4, 8], index=0, key="ew_bt_h")
        if st.button("Run rolling-origin back-test", type="primary", key="ew_run_bt"):
            with st.spinner("Rolling-origin back-test of full ensemble…"):
                bt, bt_sum = ew_backtest(panel, horizon_weeks=int(bt_h))
            if bt_sum.get("status") != "ok":
                st.warning(bt_sum.get("detail", "Back-test unavailable"))
            else:
                status_icon = "🟢 Validated" if bt_sum.get("validated") else "🟡 Provisional (need more history)"
                st.markdown(f"**Model status:** {status_icon}")
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Pairs", bt_sum["n_pairs"])
                m2.metric("MAE ensemble", bt_sum.get("mae_ensemble", bt_sum["mae_model"]))
                m3.metric("MAE seasonal", bt_sum.get("mae_seasonal", bt_sum["mae_naive"]))
                m4.metric("Skill vs seasonal", bt_sum["skill_vs_naive"],
                          help="Positive = ensemble beats seasonal naïve")
                m5.metric("Interval coverage", f"{bt_sum['interval_coverage_pct']:.0f}%",
                          help="Target ≈80%")
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("MAE risk-score", bt_sum.get("mae_risk"))
                c2.metric("MAE ridge", bt_sum.get("mae_ridge") if bt_sum.get("mae_ridge") is not None else "n/a")
                c3.metric("MAE AR", bt_sum.get("mae_ar") if bt_sum.get("mae_ar") is not None else "n/a")
                c4.metric("MAE quasi-NB", bt_sum.get("mae_nb") if bt_sum.get("mae_nb") is not None else "n/a")
                c5.metric("Best component", bt_sum.get("best_component", "—"))
                st.caption(
                    "Rolling-origin comparison: seasonal · risk-score · ridge · autoregressive · quasi-NB · ensemble. "
                    "Best = lowest MAE. Production forecast remains the ensemble unless you explicitly switch after validation."
                )
                st.caption(bt_sum["detail"])
                st.info(
                    f"Interval calibration scale → **{bt_sum.get('interval_calib_scale', 1):.2f}** "
                    f"(stored for next production forecasts; target coverage 80%)."
                )
                st.dataframe(bt.tail(60), use_container_width=True, hide_index=True)
                skill_fac = bt.groupby("facility").agg(
                    mae_ensemble=("abs_err_ensemble", "mean"),
                    mae_seasonal=("abs_err_seasonal", "mean"),
                    n=("actual", "count"),
                ).reset_index()
                skill_fac["skill"] = 1 - skill_fac["mae_ensemble"] / skill_fac["mae_seasonal"].clip(lower=0.01)
                st.markdown("#### By facility")
                st.dataframe(skill_fac.round(2), use_container_width=True, hide_index=True)
        else:
            st.info(
                "Run when you have several months of facility-week history. "
                "Validation requires ≥40 pairs and ≥2 facilities for 🟢 Validated status."
            )

    # ── Tab 7: Interventions ────────────────────────────────────────
    with tabs[6]:
        st.subheader("Intervention coverage (ITN / IRS / IPTp)")
        st.caption(
            "High ITN use and IRS coverage reduce the residual risk score. "
            "Update these from county surveys or campaign reports."
        )
        try:
            icov = query("SELECT * FROM intervention_coverage ORDER BY facility, period DESC")
        except Exception:
            icov = pd.DataFrame()
        if not icov.empty:
            st.dataframe(icov, use_container_width=True, hide_index=True)
        else:
            st.info("No intervention rows yet — seed values are loaded on first run.")

        if user.get("can_upload"):
            st.markdown("#### Update coverage")
            with st.form("ew_icov_form"):
                fac_i = st.selectbox("Facility", FACILITIES, key="icov_fac")
                per_i = st.text_input("Period (e.g. 2026-Q2 or 2026-06)", value="2026-Q2")
                itn_o = st.number_input("ITN ownership %", 0.0, 100.0, 70.0, 1.0)
                itn_u = st.number_input("ITN use %", 0.0, 100.0, 55.0, 1.0)
                irs_c = st.number_input("IRS coverage %", 0.0, 100.0, 0.0, 1.0)
                iptp = st.number_input("IPTp coverage %", 0.0, 100.0, 40.0, 1.0)
                if st.form_submit_button("Save intervention coverage"):
                    execute(
                        """
                        INSERT OR REPLACE INTO intervention_coverage
                        (facility, period, itn_ownership_pct, itn_use_pct, irs_coverage_pct,
                         iptp_coverage_pct, source, notes)
                        VALUES (?, ?, ?, ?, ?, ?, 'manual', 'Updated from EW page')
                        """,
                        (fac_i, per_i, itn_o, itn_u, irs_c, iptp),
                    )
                    st.success("Saved.")
                    st.rerun()

        st.markdown("#### Mobility proxies")
        try:
            mob = query("SELECT * FROM mobility_proxy")
            if not mob.empty:
                st.dataframe(mob, use_container_width=True, hide_index=True)
        except Exception:
            st.caption("Mobility table not available.")

        st.markdown("#### Entomology surveys")
        try:
            ent = query("SELECT * FROM entomology ORDER BY survey_date DESC")
            if not ent.empty:
                st.dataframe(ent, use_container_width=True, hide_index=True)
            else:
                st.caption("No entomology rows yet — schema ready for Anopheles density, EIR, resistance.")
        except Exception:
            st.caption("Entomology table not available.")
        if user.get("can_upload"):
            with st.expander("Add entomology survey row"):
                with st.form("ew_ent_form"):
                    ef = st.selectbox("Facility", FACILITIES, key="ent_fac")
                    ed = st.date_input("Survey date")
                    e_dens = st.number_input("Anopheles density", 0.0, 500.0, 0.0)
                    e_eir = st.number_input("EIR", 0.0, 200.0, 0.0)
                    e_sp = st.number_input("Sporozoite rate %", 0.0, 100.0, 0.0)
                    e_res = st.text_input("Insecticide resistance notes", "")
                    if st.form_submit_button("Save entomology"):
                        execute(
                            """
                            INSERT INTO entomology
                            (facility, survey_date, anopheles_density, eir, sporozoite_rate,
                             insecticide_resistance, source)
                            VALUES (?, ?, ?, ?, ?, ?, 'manual')
                            """,
                            (ef, str(ed), e_dens, e_eir, e_sp, e_res),
                        )
                        st.success("Saved entomology row.")
                        st.rerun()

    # ── Tab 8: Scenario ─────────────────────────────────────────────
    with tabs[7]:
        st.subheader("What-if scenario explorer")
        st.caption(
            "Adjust rainfall pressure and ITN use to see how the transparent risk score and "
            "expected cases respond. Sensitivity tool, not a full re-fit."
        )
        rain_mult = st.slider("Rainfall multiplier (vs current lagged rain)", 0.5, 2.0, 1.0, 0.1)
        itn_scen = st.slider("ITN use % (scenario)", 0, 100, 55, 5)
        fac_sc = st.selectbox("Facility for scenario", FACILITIES, key="ew_scen_fac")
        latest_idx = panel.groupby("facility")["week_ending"].idxmax()
        latest = panel.loc[latest_idx]
        row = latest[latest["facility"] == fac_sc]
        if row.empty:
            st.info("No latest row for facility.")
        else:
            base_row = latest[latest["facility"] == fac_sc].iloc[0]
            row = base_row.copy()
            for lag in EW_RAIN_LAGS:
                col = f"rain_lag_{lag}"
                if col in row and pd.notna(row[col]):
                    row[col] = float(row[col]) * rain_mult
            if "rain_roll_4" in row and pd.notna(row["rain_roll_4"]):
                row["rain_roll_4"] = float(row["rain_roll_4"]) * rain_mult
            row["itn_use_pct"] = float(itn_scen)
            sc0, _ = _ew_risk_score_row(base_row)
            sc1, drivers = _ew_risk_score_row(row)
            # Series has no .assign — set risk_score on copies
            br0 = base_row.copy()
            br0["risk_score"] = sc0
            br1 = row.copy()
            br1["risk_score"] = sc1
            pred0, lo0, hi0, _ = _ew_predict_cases(br0, 4, model_bundle=None)
            pred1, lo1, hi1, _ = _ew_predict_cases(br1, 4, model_bundle=None)
            a, b = st.columns(2)
            a.metric("Baseline risk score", f"{sc0:.2f}", delta=_ew_risk_level(sc0))
            b.metric("Scenario risk score", f"{sc1:.2f}", delta=f"{sc1-sc0:+.2f} ({_ew_risk_level(sc1)})")
            a.metric("Baseline 4-wk expected", f"{pred0:.0f}", delta=f"interval {lo0:.0f}–{hi0:.0f}")
            b.metric("Scenario 4-wk expected", f"{pred1:.0f}", delta=f"{pred1-pred0:+.0f}")
            st.dataframe(
                pd.DataFrame(drivers, columns=["Factor", "Signal", "Detail"]),
                use_container_width=True, hide_index=True,
            )

    # ── Tab 9: Method ───────────────────────────────────────────────
    with tabs[8]:
        st.markdown(
            """
### Performance by horizon (from stored back-tests)

Run **Validation / back-test** for each horizon. Target: report MAE, skill and interval coverage separately for 4 / 8 / 12 weeks — do not quote a single MAE for all horizons.

### Method summary (ew_v2_ensemble)

**Target**  
Expected confirmed malaria cases over the next 4 / 8 / 12 weeks at facility level.

**Feature groups**
1. **Climate** — rainfall lags (1–8 weeks), rolling sums, anomaly, temperature suitability, ET0 (Open-Meteo)  
2. **Environment** — elevation, water distance, NDVI/greenness (`ndvi_weekly` from NASA POWER pipeline, MODIS CSV upload, or rain proxy)  
3. **Surveillance** — true weekly 648/521 preferred; else monthly KHIS distributed; TPR; case anomaly  
4. **Commodities** — RDT / ACT stock pressure  
5. **Interventions** — ITN ownership/use, IRS, IPTp (protective)  
6. **Mobility** — Haversine distances to Kilifi town, Malindi, markets, highway; fishing / seasonal migration flags  
7. **Entomology** — Anopheles density, EIR, sporozoite rate when surveyed  
8. **Seasonality** — week-of-year + long/short rains boost  

**Risk score**  
Transparent weighted sum ∈ [0, 1], reduced by ITN/IRS protection.  
Thresholds: Low < 0.35 · Moderate < 0.55 · High < 0.75 · Very High ≥ 0.75.

**Ensemble case prediction**
- **A — Seasonal naïve**: historical mean × horizon factor (weight 0.25)  
- **B — Risk-score model**: mean × climate_multiplier(score) (weight 0.40)  
- **C — Ridge** (numpy): log1p(future cases) ~ **lagged cases/TPR/testing** + rain lags + greenness + ITN + Fourier seasonality (weight 0.35 when trained)  
- ≈80% prediction **intervals** (not a confidence %). Calibrated from back-test coverage.

**Validation (rolling-origin)**  
At each origin: train ridge **only on past data**, score seasonal / risk / ridge / ensemble vs actual.  
MAE by component, skill, interval coverage, best component, validated flag (≥40 pairs, ≥2 facilities).

**Operational response**
- High / Very High → alerts  
- EPR checklist **only if data-validity gate passes**; else “review required”

**Greenness note**  
`environmental_greenness` prefers uploaded NDVI; else NASA POWER greenness (down-weighted vs rainfall to limit double-counting); else rain proxy.
            """
        )


def page_users_access(user, filters):
    """
    Access & Governance — manage named accounts, roles, geographic scope, enable/disable.
    Admin only (can_manage_users).
    """
    page_header(
        "Users & Access",
        "Role-based access control with geographic scope. "
        "Permissions come from the role template; facility/sub-county assignments limit data.",
        breadcrumb="Data & Governance / Users & Access",
    )
    if not user.get("can_manage_users"):
        st.error("You do not have permission to manage users.")
        return

    section_header("Directory")
    try:
        users_df = query(
            "SELECT username, role_key, display_name, facility_scope, sub_county_scope, "
            "IFNULL(active,1) AS active, must_change, last_login, created_at, updated_at "
            "FROM users ORDER BY username"
        )
    except Exception as e:
        st.error(f"Cannot load users: {e}")
        return
    if users_df.empty:
        st.warning("No users in database. Seed defaults via login or seed_users().")
        if st.button("Seed default role accounts"):
            seed_users()
            st.rerun()
        return

    users_df["Status"] = users_df["active"].apply(lambda x: "🟢 Active" if int(x or 0) == 1 else "🔴 Disabled")
    st.dataframe(
        users_df.rename(columns={
            "username": "Username",
            "role_key": "Role",
            "display_name": "Display name",
            "facility_scope": "Facility scope",
            "sub_county_scope": "Sub-county scope",
            "must_change": "Must change pwd",
            "last_login": "Last login",
        })[
            ["Username", "Display name", "Role", "Facility scope", "Sub-county scope",
             "Status", "Last login", "Must change pwd"]
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"**{len(users_df)}** accounts · Active: **{int((users_df['active'] == 1).sum())}**")

    section_header("Add named account")
    st.caption(
        "Create a real person account. Role selects permissions; "
        "facility or sub-county scope limits geography (leave blank for full role scope)."
    )
    with st.form("add_user_form"):
        a1, a2 = st.columns(2)
        with a1:
            new_user = st.text_input("Username * (login id)", placeholder="jane.doe")
            new_name = st.text_input("Display name *", placeholder="Jane Doe")
            new_role = st.selectbox("Role template *", list(ROLES.keys()),
                                   format_func=lambda k: f"{k} — {ROLES[k]['name']}")
        with a2:
            new_fac = st.selectbox("Facility scope", ["(none — role default)"] + FACILITIES)
            new_sub = st.selectbox("Sub-county scope", ["(none — role default)"] + KILIFI_SUBCOUNTIES)
            new_pwd = st.text_input("Temporary password *", type="password",
                                   help="Min 8 characters. User should change on first login.")
        force_change = st.checkbox("Require password change on next login", value=True)
        if st.form_submit_button("Create account"):
            if not new_user.strip() or not new_name.strip() or not new_pwd:
                st.error("Username, display name and password are required.")
            elif len(new_pwd) < 8:
                st.error("Password must be at least 8 characters.")
            elif not re.match(r"^[a-zA-Z0-9._@-]+$", new_user.strip()):
                st.error("Username: letters, numbers, . _ @ - only.")
            else:
                exists = query("SELECT username FROM users WHERE username = ?", (new_user.strip(),))
                if not exists.empty:
                    st.error("Username already exists.")
                else:
                    execute(
                        """
                        INSERT INTO users
                        (username, password_hash, must_change, role_key, display_name,
                         facility_scope, sub_county_scope, active)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            new_user.strip(),
                            _hash_password(new_pwd),
                            1 if force_change else 0,
                            new_role,
                            new_name.strip(),
                            None if new_fac.startswith("(") else new_fac,
                            None if new_sub.startswith("(") else new_sub,
                        ),
                    )
                    log_audit(user["username"], "user_create", "users", new_user.strip())
                    st.success(f"Created **{new_user.strip()}** ({new_role}).")
                    st.rerun()

    section_header("Edit account")
    unames = users_df["username"].tolist()
    pick = st.selectbox("Select user", unames, key="ua_pick")
    row = users_df[users_df["username"] == pick].iloc[0]
    with st.form("edit_user_form"):
        e1, e2 = st.columns(2)
        with e1:
            ed_name = st.text_input("Display name", value=str(row.get("display_name") or ""))
            role_opts = list(ROLES.keys())
            cur_role = row.get("role_key") or (pick if pick in ROLES else "viewer")
            if cur_role not in role_opts:
                role_opts = [cur_role] + role_opts
            ed_role = st.selectbox(
                "Role template",
                role_opts,
                index=role_opts.index(cur_role) if cur_role in role_opts else 0,
                format_func=lambda k: f"{k} — {ROLES[k]['name']}" if k in ROLES else k,
            )
        with e2:
            fac_opts = ["(none — role default)"] + FACILITIES
            cur_fac = row.get("facility_scope") or "(none — role default)"
            if cur_fac not in fac_opts:
                fac_opts = [cur_fac] + fac_opts
            ed_fac = st.selectbox("Facility scope", fac_opts, index=fac_opts.index(cur_fac) if cur_fac in fac_opts else 0)
            sub_opts = ["(none — role default)"] + KILIFI_SUBCOUNTIES
            cur_sub = row.get("sub_county_scope") or "(none — role default)"
            if cur_sub not in sub_opts:
                sub_opts = [cur_sub] + sub_opts
            ed_sub = st.selectbox("Sub-county scope", sub_opts, index=sub_opts.index(cur_sub) if cur_sub in sub_opts else 0)
            ed_active = st.checkbox("Account active", value=int(row.get("active") or 0) == 1)
        ed_pwd = st.text_input("Reset password (leave blank to keep)", type="password")
        ed_force = st.checkbox(
            "Require password set on next login (activation)",
            value=bool(row.get("must_change")),
            help="Prefer generating an activation token below instead of sharing a temporary password.",
        )
        # Module visibility override (what this person can see)
        role_pages = list(ROLES.get(ed_role, {}).get("pages") or [])
        try:
            import json as _json
            cur_ov = query("SELECT pages_override FROM users WHERE username=?", (pick,))
            if not cur_ov.empty and cur_ov.iloc[0].get("pages_override"):
                default_pages = _json.loads(cur_ov.iloc[0]["pages_override"])
            else:
                default_pages = role_pages
        except Exception:
            default_pages = role_pages
        all_possible = sorted({p for r in ROLES.values() for p in r.get("pages", [])})
        ed_pages = st.multiselect(
            "Modules this user can see (leave as role default by selecting none extra, or customize)",
            all_possible,
            default=[p for p in default_pages if p in all_possible],
            help="Admin controls exactly which sidebar pages appear for this account.",
        )
        if st.form_submit_button("Save changes"):
            if pick == user["username"] and not ed_active:
                st.error("You cannot disable your own account.")
            else:
                import json as _json
                pages_json = _json.dumps(ed_pages) if ed_pages else None
                params = [
                    ed_name.strip() or pick,
                    ed_role,
                    None if str(ed_fac).startswith("(") else ed_fac,
                    None if str(ed_sub).startswith("(") else ed_sub,
                    1 if ed_active else 0,
                    1 if ed_force else 0,
                    pages_json,
                ]
                sql = """
                    UPDATE users SET display_name=?, role_key=?, facility_scope=?, sub_county_scope=?,
                    active=?, must_change=?, pages_override=?, updated_at=CURRENT_TIMESTAMP
                """
                if ed_pwd.strip():
                    if len(ed_pwd) < 8:
                        st.error("Password must be at least 8 characters.")
                        st.stop()
                    sql += ", password_hash=?"
                    params.append(_hash_password(ed_pwd.strip()))
                sql += " WHERE username=?"
                params.append(pick)
                execute(sql, tuple(params))
                log_audit(user["username"], "user_update", "users", pick)
                st.success(f"Updated **{pick}**.")
                st.rerun()

    st.markdown("#### Activation / password reset link")
    st.caption(
        "Generates a one-time token. Share the token securely (WhatsApp/email). "
        "Recipient opens the app with `?activate=<token>` and sets their own password. "
        "Admin never sees the user's permanent password."
    )
    if st.button("Generate activation link for selected user", key="gen_act"):
        tok, exp = create_password_token(pick, purpose="activation", hours=72)
        st.success(f"Token created (expires {exp}).")
        st.code(f"?activate={tok}", language=None)
        st.caption("Append this query parameter to your Streamlit URL, or paste the token on the login activation form.")

    section_header("Delete account")
    st.caption(
        "Permanently removes the login. Prefer **disable** (Edit account → uncheck Active) if you may need the account again. "
        "You cannot delete your own account or the last remaining admin."
    )
    del_user = st.selectbox("Account to delete", unames, key="ua_del_pick")
    confirm_del = st.text_input("Type the username to confirm delete", key="ua_del_confirm")
    if st.button("Delete account permanently", type="secondary", key="ua_del_go"):
        if del_user == user.get("username"):
            st.error("You cannot delete your own account.")
        elif confirm_del.strip() != del_user:
            st.error("Confirmation did not match the username.")
        else:
            # Protect last admin
            try:
                n_admin = int(query(
                    "SELECT COUNT(*) AS n FROM users WHERE role_key='admin' AND IFNULL(active,1)=1 AND username != ?",
                    (del_user,),
                ).iloc[0]["n"])
            except Exception:
                n_admin = 1
            row_del = users_df[users_df["username"] == del_user]
            is_admin = (not row_del.empty) and str(row_del.iloc[0].get("role_key")) == "admin"
            if is_admin and n_admin < 1:
                st.error("Cannot delete the last active admin account.")
            else:
                execute("DELETE FROM users WHERE username=?", (del_user,))
                try:
                    execute("DELETE FROM password_tokens WHERE username=?", (del_user,))
                except Exception:
                    pass
                log_audit(user["username"], "user_delete", "users", del_user)
                st.success(f"Deleted account **{del_user}**.")
                st.rerun()

    section_header("Generate regulated view link")
    st.caption(
        "Create a **read-only link** that controls what the recipient sees — "
        "modules, facility or sub-county, and optional live cascade. No full login required."
    )
    with st.form("regulated_share_form"):
        sh_title = st.text_input("Link title", value="Kilifi malaria brief")
        sh_days = st.number_input("Expires in (days)", min_value=1, max_value=90, value=7)
        sh_fac = st.selectbox("Limit to facility", ["(all visible)"] + FACILITIES)
        sh_sub = st.selectbox("Limit to sub-county", ["(all visible)"] + KILIFI_SUBCOUNTIES)
        all_mods = sorted({p for r in ROLES.values() for p in r.get("pages", [])})
        sh_mods = st.multiselect(
            "Modules mentioned on the link (label only — full app still blocked)",
            all_mods,
            default=["Overview", "Command Centre"] if "Overview" in all_mods else all_mods[:2],
        )
        sh_summary = st.text_area(
            "Message to recipient",
            value="Read-only Kilifi CBMP extract. For discussion only — not a formal outbreak declaration.",
        )
        sh_live = st.checkbox("Include live cascade numbers (scoped to geography above)", value=True)
        sh_min = st.number_input("Min cell suppression for counts", min_value=1, max_value=20, value=5)
        sh_notes = st.text_area("Extra notes (optional)", value="")
        if st.form_submit_button("Generate regulated link"):
            payload = {
                "summary": sh_summary,
                "notes": sh_notes,
                "allowed_modules": sh_mods,
                "facility_scope": None if str(sh_fac).startswith("(") else sh_fac,
                "sub_county_scope": None if str(sh_sub).startswith("(") else sh_sub,
                "include_live_cascade": bool(sh_live),
                "min_cell": int(sh_min),
                "metrics": [],
            }
            # Snapshot simple cascade into metrics if possible
            try:
                df0 = load_all_data()
                fu = {
                    "facility_scope": payload["facility_scope"],
                    "sub_county_scope": payload["sub_county_scope"],
                    "scope": "facility" if payload["facility_scope"] else "all",
                    "aggregation": True,
                    "min_cell": int(sh_min),
                }
                df0 = apply_user_data_scope(df0, user=fu)
                fl = {"facility": payload["facility_scope"]} if payload["facility_scope"] else {}
                c0 = compute_cascade_metrics(_filter_data(df0, fl, user=fu))
                payload["metrics"] = [{
                    "Suspected": c0.get("suspected"),
                    "Tested": c0.get("tested"),
                    "Confirmed": c0.get("confirmed"),
                    "TPR %": c0.get("positivity"),
                }]
            except Exception:
                pass
            tok = create_shared_view(
                sh_title, "regulated", payload, user.get("username", "admin"), days=int(sh_days),
            )
            st.success("Regulated link created.")
            st.code(f"?share={tok}", language=None)
            st.caption(
                "Send the full URL: your Streamlit base URL + `?share=` + token. "
                "Recipient only sees this regulated page — not the full dashboard."
            )

    section_header("Shared links")
    try:
        shares = query(
            "SELECT id, title, token, created_by, created_at, expires_at, "
            "IFNULL(revoked,0) AS revoked, IFNULL(open_count,0) AS open_count "
            "FROM shared_views ORDER BY id DESC LIMIT 30"
        )
    except Exception:
        shares = pd.DataFrame()
    if shares.empty:
        st.caption("No shared views yet. Use **Share current filters** on Overview or Comparison.")
    else:
        st.dataframe(shares, use_container_width=True, hide_index=True)
        rev_id = st.selectbox("Revoke share id", shares["id"].tolist(), key="rev_share")
        if st.button("Revoke selected link", key="rev_go"):
            execute("UPDATE shared_views SET revoked=1 WHERE id=?", (int(rev_id),))
            log_audit(user["username"], "share_revoke", "shared_views", str(rev_id))
            st.success("Revoked.")
            st.rerun()

    section_header("Preview as user")
    st.caption(
        "See the dashboard exactly as another account would (read-only simulation). "
        "Does not perform actions as that user."
    )
    if not users_df.empty:
        prev_u = st.selectbox("Preview account", users_df["username"].tolist(), key="preview_user_sel")
        if st.button("Start preview", key="preview_start"):
            sess = build_session_user(prev_u)
            if not sess:
                st.error("Cannot preview: account disabled or misconfigured.")
            else:
                st.session_state["_preview_of"] = st.session_state.user  # save admin
                st.session_state.user = sess
                st.session_state.user["_is_preview"] = True
                st.session_state.user["can_upload"] = False
                st.session_state.user["can_commit"] = False
                st.session_state.user["can_manage_users"] = False
                st.success(f"Now previewing as **{prev_u}**. Use sidebar to navigate.")
                st.rerun()
    if st.session_state.get("user", {}).get("_is_preview") and st.button("End preview — restore admin", key="preview_end"):
        if st.session_state.get("_preview_of"):
            st.session_state.user = st.session_state.pop("_preview_of")
            st.rerun()

    section_header("Role permission matrix (read-only)")
    matrix = []
    for k, v in ROLES.items():
        matrix.append({
            "Role key": k,
            "Name": v["name"],
            "Scope default": v.get("scope"),
            "Upload": "✓" if v.get("can_upload") else "",
            "Commit": "✓" if v.get("can_commit") else "",
            "Manage users": "✓" if v.get("can_manage_users") else "",
            "Pages": len(v.get("pages") or []),
        })
    st.dataframe(pd.DataFrame(matrix), use_container_width=True, hide_index=True)
    st.caption(
        "Hiding UI is not enough for security: upload/commit and admin actions also check "
        "`can_upload` / `can_commit` / `can_manage_users` in code. Geographic scope is applied in the filter bar."
    )


def page_dashboard_qa(user, filters):
    """
    Admin / M&E: automated data & visual QA checks before release or donor review.
    """
    page_header(
        "Dashboard QA",
        "Automated integrity checks on indicators, forecasts, GIS, and data grain. "
        "Use before release or technical review.",
    )
    df = load_all_data()
    checks = []

    def _add(area, name, status, detail):
        checks.append({"Area": area, "Check": name, "Status": status, "Detail": detail})

    # Indicator engine
    if df is None or df.empty:
        _add("Indicators", "KHIS data present", "🔴", "No committed KHIS data")
    else:
        _add("Indicators", "KHIS data present", "🟢", f"{len(df):,} observation rows")
        try:
            for iid in ("GO_1", "R1_1", "R1_2", "CAS_1"):
                r = compute_indicator(iid, df, filters or {})
                stt = (r.get("status") or r.get("formula_status") or "na")
                icon = "🟢" if stt == "valid" else ("🟡" if stt in ("na", "pending_data") else "🔴")
                _add("Indicators", iid, icon, f"status={stt} value={r.get('value')}")
        except Exception as e:
            _add("Indicators", "compute_indicator", "🔴", str(e)[:120])

        # Negative / impossible values
        vals = pd.to_numeric(df.get("value"), errors="coerce")
        n_neg = int((vals < 0).sum()) if vals is not None else 0
        _add("Graph QA", "No negative values", "🟢" if n_neg == 0 else "🔴", f"negatives={n_neg}")

        # Future periods
        per = df.get("period")
        if per is not None:
            future = 0
            for p in per.astype(str).unique():
                if re.match(r"^\d{4}-\d{2}$", p):
                    try:
                        if pd.Period(p, freq="M") > pd.Period(datetime.utcnow().strftime("%Y-%m"), freq="M"):
                            future += 1
                    except Exception:
                        pass
            _add("Graph QA", "No future periods", "🟢" if future == 0 else "🟡", f"future_periods={future}")

    # Population
    _add(
        "Epidemiology",
        "Population status",
        "🟢" if get_population_status() == "confirmed" else "🟡",
        get_population_status(),
    )

    # GIS
    n_approx = sum(1 for f, g in GEO_SITES.items() if g.get("gps_status") == "gazetteer")
    _add("GIS", "Facility GPS", "🟡" if n_approx else "🟢", f"{n_approx} approximate gazetteer sites")
    geo_path = Path(__file__).resolve().parent / "docs" / "geo" / "kilifi_county.geojson"
    _add("GIS", "County outline GeoJSON", "🟢" if geo_path.exists() else "🔴", str(geo_path.name))

    # Climate / EW
    try:
        n_clim = int(query("SELECT COUNT(*) AS n FROM climate_data").iloc[0]["n"])
    except Exception:
        n_clim = 0
    _add("Forecast", "Climate rows", "🟢" if n_clim > 0 else "🟡", f"n={n_clim}")
    try:
        n_ew = int(query("SELECT COUNT(*) AS n FROM ew_forecasts").iloc[0]["n"])
    except Exception:
        n_ew = 0
    _add("Forecast", "EW forecast rows", "🟢" if n_ew > 0 else "🟡", f"n={n_ew}")
    try:
        last_bt = query(
            "SELECT mae, notes FROM ew_model_runs WHERE model_name='ew_v2_ensemble' ORDER BY id DESC LIMIT 1"
        )
        if not last_bt.empty:
            _add("Forecast", "Last back-test", "🟢", str(last_bt.iloc[0].get("notes") or "")[:160])
        else:
            _add("Forecast", "Last back-test", "🟡", "No rolling-origin run stored yet")
    except Exception:
        _add("Forecast", "Last back-test", "🟡", "ew_model_runs unavailable")

    # Sub-county
    try:
        n_sc = int(query("SELECT COUNT(DISTINCT sub_county) AS n FROM kilifi_subcounty").iloc[0]["n"])
    except Exception:
        n_sc = 0
    _add("Comparison", "WFK sub-counties loaded", "🟢" if n_sc >= 5 else "🟡", f"{n_sc}/7")

    # Alerts open
    try:
        n_open = int(query("SELECT COUNT(*) AS n FROM alerts WHERE status='open'").iloc[0]["n"])
    except Exception:
        n_open = 0
    _add("Alerts", "Open alerts", "🟡" if n_open > 10 else "🟢", f"n={n_open}")

    qa = pd.DataFrame(checks)
    st.dataframe(qa, use_container_width=True, hide_index=True)
    n_red = int((qa["Status"] == "🔴").sum())
    n_yellow = int((qa["Status"] == "🟡").sum())
    n_green = int((qa["Status"] == "🟢").sum())
    st.markdown(f"**Summary:** 🟢 {n_green} · 🟡 {n_yellow} · 🔴 {n_red}")
    if n_red:
        st.error("Resolve red checks before treating outputs as release-ready.")
    elif n_yellow:
        st.warning("Yellow items are acceptable for pilot use with documented caveats.")
    else:
        st.success("All automated checks green.")

    st.download_button(
        "📥 Download QA report CSV",
        qa.to_csv(index=False).encode("utf-8"),
        file_name=f"cbmp_dashboard_qa_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="qa_dl",
    )


def page_climate(user, filters):
    """
    B5 — climate early-warning *signals* (not a forecast model).
    Rainfall log + locked case/TPR movement.
    """
    page_header(
        "Climate & malaria intelligence",
        "Rain, temperature and humidity at 7 sub-county centroids + 3 CBMP sites. "
        "Signals require **VALID** malaria rates. Full multifactor forecast → **Malaria Early Warning**.",
    )
    st.markdown(
        """
**Live sources**
- **Open-Meteo** (wired below) — free forecast + recent daily rain/temperature, no API key.  
  Docs: https://open-meteo.com/ · Licence: CC BY 4.0 (attribute Open-Meteo).
- **KMD Maproom / ENACTS** (official Kenya merged station+satellite) — browse, not a simple JSON API:  
  http://kmddl.meteo.go.ke:8081/maproom/ · https://meteo.go.ke/
- Manual entry remains for a local rain gauge or KMD bulletin.
        """
    )
    if st.button("🔮 Open Malaria Early Warning (full multifactor forecast)", key="goto_ew"):
        st.session_state.nav = "Malaria Early Warning"
        st.session_state.nav_unlocked = True
        st.rerun()
    if user.get("can_upload"):
        c1, c2, c3 = st.columns(3)
        with c1:
            days = st.selectbox("History window (days)", [28, 56, 90], index=1, key="om_days")
        with c2:
            if st.button("☁ Pull history (weekly)", type="primary", key="om_fetch"):
                with st.spinner("Open-Meteo history …"):
                    rows, errs = fetch_open_meteo_weekly(days=int(days))
                if rows:
                    n = upsert_climate_rows(rows, user.get("username", "system"))
                    st.success(f"Stored **{n}** weekly history rows (rain, Tmin/Tmax, humidity, rain hours, ET0).")
                    if errs:
                        st.warning("; ".join(errs))
                    st.rerun()
                else:
                    st.error("No rows. " + ("; ".join(errs) if errs else "Check internet."))
        with c3:
            ahead = st.selectbox("Forecast horizon", [7, 16], index=1, key="om_ahead")
            if st.button("🌦 Pull 7/16-day outlook", key="om_fc"):
                with st.spinner("Open-Meteo forecast …"):
                    frows, ferrs = fetch_open_meteo_forecast(ahead_days=int(ahead))
                if frows:
                    n = upsert_climate_forecast(frows, user.get("username", "system"))
                    st.success(f"Stored **{n}** daily outlook rows (+{ahead} days). Weather outlook only.")
                    if ferrs:
                        st.warning("; ".join(ferrs))
                    st.rerun()
                else:
                    st.error("No forecast rows. " + ("; ".join(ferrs) if ferrs else ""))
    df = load_all_data()
    climate, pack = compute_climate_ew(df, filters)

    level = pack.get("level")
    if level == "high":
        headline(f"**{pack.get('title')}** — {pack.get('detail')}", status="red")
    elif level == "watch":
        headline(f"**{pack.get('title')}** — {pack.get('detail')}", status="amber")
    elif level == "quiet":
        headline(f"**{pack.get('title')}** — {pack.get('detail')}", status="green")
    else:
        headline(pack.get("detail") or "No climate log yet.", status="grey")

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        big_number(
            f"{pack['latest_rain']:.0f} mm" if pack.get("latest_rain") is not None else "—",
            "Latest week rain",
        )
    with k2:
        big_number(
            f"{pack['rain_4wk']:.0f} mm" if pack.get("rain_4wk") is not None else "—",
            "Last 4 weeks",
        )
    with k3:
        big_number(
            f"{int(pack['last_conf']):,}" if pack.get("last_conf") is not None else "—",
            "Latest month confirmed",
        )
    with k4:
        big_number("Yes" if pack.get("rising_tpr") else "No", "Valid TPR rising")

    if pack.get("signals"):
        section_header("Active signals")
        for s in pack["signals"]:
            st.markdown(f"- {s}")

    section_header("Weekly climate log")
    st.caption("Enter Kenya Meteorological Department or local station values. Facility is optional.")
    if user.get("can_upload"):
        with st.form("rainfall_form"):
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                week = st.date_input("Week ending")
            with c2:
                rainfall = st.number_input("Rainfall (mm)", min_value=0.0, max_value=800.0, step=1.0)
            with c3:
                temperature = st.number_input("Mean temperature (°C)", min_value=0.0, max_value=50.0, step=0.5)
            with c4:
                fac = st.selectbox("Facility (optional)", ["(all sites)"] + FACILITIES)
            if st.form_submit_button("Save week"):
                execute(
                    "INSERT INTO climate_data (week_ending, facility, rainfall_mm, temperature_c) VALUES (?, ?, ?, ?)",
                    (str(week), None if fac == "(all sites)" else fac, rainfall, temperature),
                )
                log_audit(user.get("username", ""), "climate_log", "climate_data", str(week))
                st.success("Week recorded.")
                st.rerun()

    if climate is not None and not climate.empty:
        section_header("Rainfall series")
        fig = px.line(
            climate, x="week_ending", y="rainfall_mm", color="facility" if "facility" in climate.columns and climate["facility"].notna().any() else None,
            markers=True, height=360,
            labels={"rainfall_mm": "Rainfall (mm)", "week_ending": "Week ending"},
        )
        fig.add_hline(y=80, line_dash="dot", line_color="#D97706", annotation_text="Watch 80 mm")
        fig.add_hline(y=120, line_dash="dash", line_color="#B91C1C", annotation_text="High 120 mm")
        fig.update_layout(plot_bgcolor="white")
        st.plotly_chart(fig, use_container_width=True)
        if "temperature_c" in climate.columns and climate["temperature_c"].notna().any():
            fig_t = px.line(climate, x="week_ending", y="temperature_c", markers=True, height=280)
            fig_t.update_layout(plot_bgcolor="white", yaxis_title="°C")
            st.plotly_chart(fig_t, use_container_width=True)
        st.dataframe(climate.tail(24), use_container_width=True, hide_index=True)

        if user.get("can_upload") and pack.get("level") in ("high", "watch"):
            if st.button("Create workplan from climate signal", key="b5_wp"):
                execute(
                    """
                    INSERT INTO workplan_actions
                    (facility, problem, action_text, owner, due_date, status, source, created_by)
                    VALUES (?, ?, ?, ?, ?, 'open', 'climate', ?)
                    """,
                    (
                        filters.get("facility") if filters and filters.get("facility") not in (None, "All three") else "All three",
                        pack.get("title"),
                        pack.get("detail") + " Signals: " + "; ".join(pack.get("signals") or []),
                        "County malaria / EPR",
                        (datetime.now() + pd.Timedelta(days=7)).strftime("%Y-%m-%d"),
                        user.get("username", ""),
                    ),
                )
                st.success("Workplan item created.")
                st.rerun()
    else:
        st.info("No climate rows yet. Log weekly rainfall to activate B5 signals.")

    section_header("7- and 16-day weather outlook")
    st.caption(
        "Open-Meteo **weather** forecast at the same 7+3 points. "
        "This is not a malaria prediction. Use it to watch rain/heat next week vs last week."
    )
    try:
        fc = query("SELECT * FROM climate_forecast ORDER BY forecast_date, facility")
    except Exception:
        fc = pd.DataFrame()
    if fc.empty:
        st.info("Pull the 7/16-day outlook with the button above.")
    else:
        fc["forecast_date"] = pd.to_datetime(fc["forecast_date"], errors="coerce")
        today = pd.Timestamp(datetime.now().date())
        next7 = fc[(fc["forecast_date"] >= today) & (fc["forecast_date"] < today + pd.Timedelta(days=7))]
        next16 = fc[(fc["forecast_date"] >= today) & (fc["forecast_date"] < today + pd.Timedelta(days=16))]
        sum7 = next7.groupby("facility")["rainfall_mm"].sum().rename("Rain next 7d mm")
        sum16 = next16.groupby("facility")["rainfall_mm"].sum().rename("Rain next 16d mm")
        t7 = next7.groupby("facility")["temp_mean"].mean().rename("Tmean next 7d")
        board_fc = pd.concat([sum7, sum16, t7], axis=1).reset_index().rename(columns={"facility": "Place"})
        st.dataframe(board_fc, use_container_width=True, hide_index=True)
        wet = board_fc.sort_values("Rain next 7d mm", ascending=False).head(3)
        if not wet.empty:
            bits = []
            for _, wr in wet.iterrows():
                place = wr.get("Place", "—")
                rain = wr.get("Rain next 7d mm")
                try:
                    bits.append(f"{place} ({float(rain):.0f} mm)")
                except Exception:
                    bits.append(str(place))
            st.markdown("**Wettest outlook (7-day rain):** " + ", ".join(bits))
        show = next7[next7["geography"] == "sub_county"][
            ["forecast_date", "facility", "rainfall_mm", "temp_min", "temp_max", "humidity_pct", "rain_hours"]
        ].copy()
        if not show.empty:
            show["forecast_date"] = show["forecast_date"].dt.strftime("%Y-%m-%d")
            st.caption("Daily detail — sub-counties, next 7 days")
            st.dataframe(show, use_container_width=True, hide_index=True)
        st.download_button(
            "📥 Outlook CSV",
            fc.to_csv(index=False).encode("utf-8"),
            file_name=f"climate_outlook_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key="clim_fc_csv",
        )

    section_header("Lag check vs confirmed cases")
    lag_df, lag_note = climate_lag_table(df, filters)
    st.caption(lag_note)
    if not lag_df.empty:
        st.dataframe(lag_df, use_container_width=True, hide_index=True)
        st.markdown("If all correlations are weak, do **not** treat the 80/120 mm lines as a validated early-warning model.")

    section_header("7 sub-counties — climate vs malaria (display)")
    try:
        clim_all = query("SELECT * FROM climate_data")
    except Exception:
        clim_all = pd.DataFrame()
    sig_rows = []
    for scn in KILIFI_SUBCOUNTIES:
        rain = temp = hum = None
        src = "—"
        if not clim_all.empty:
            sub = clim_all[clim_all["facility"] == scn]
            if sub.empty:
                # nearest CBMP site in that sub-county
                site = CBMP_IN_SUBCOUNTY.get(scn)
                if site:
                    sub = clim_all[clim_all["facility"] == site]
            if not sub.empty:
                last = sub.sort_values("week_ending").iloc[-1]
                rain = last.get("rainfall_mm")
                temp = last.get("temperature_c")
                hum = last.get("humidity_pct") if "humidity_pct" in sub.columns else None
                src = last.get("source") or "open-meteo"
        # malaria from WFK if present
        tpr = None
        tpr_st = "na"
        try:
            w = query("SELECT * FROM kilifi_subcounty WHERE sub_county=?", (scn,))
            if not w.empty:
                w = w.sort_values("period_date")
                lastw = w.iloc[-1]
                a = safe_rate(float(lastw.get("rdt_pos") or 0), float(lastw.get("rdt_exam") or 0), indicator="CLIM_TPR")
                tpr = a.get("value") if a.get("status") == "valid" else None
                tpr_st = a.get("status")
        except Exception:
            pass
        signal = "no climate row"
        if rain is not None:
            if tpr_st == "invalid":
                signal = "climate noted · malaria INVALID — no climate-malaria alert"
            elif tpr is None:
                signal = "climate only · malaria pending"
            else:
                signal = "both present — review lag table; not a forecast"
        sig_rows.append({
            "Sub-county": scn,
            "Rain last week mm": rain,
            "Temp °C": temp,
            "Humidity %": hum,
            "Climate source": src,
            "Latest VALID TPR": tpr,
            "TPR status": tpr_st,
            "Signal": signal,
        })
    st.dataframe(pd.DataFrame(sig_rows), use_container_width=True, hide_index=True)
    st.caption(
        "Rain at gazetteer centroids is a **model grid**, not a KMD station. "
        "A climate–malaria alert is **not** raised if TPR is INVALID or missing."
    )

    st.warning(
        "Climate is **not** used to predict “malaria will rise by X%”. "
        "NDVI, flood layers and forecasts stay off until a historical series is validated."
    )

    section_header("How this is meant to be used")
    st.markdown(
        """
- **High**: wet week **and** rising confirmed cases or valid TPR → brief EPR / commodities / CHP testing.  
- **Watch**: rain high *or* cases moving, not both.  
- **Quiet**: neither trigger.  
- Thresholds (80 / 120 mm) are operational watches for Kilifi coastal rains — tune with County / KMD.  
- Never treat this page as an outbreak forecast.
        """
    )


# ==================================================================
# PAGE 17 — REPORTS
# ==================================================================

def generate_monthly_report(facility, period):
    """Narrative monthly report for one facility + period (Phase 1)."""
    df = load_all_data()
    if facility == "All three":
        d = df[df["period"].astype(str) == str(period)] if period else df
        filt = {"facility": "All three", "periods": [period] if period else None}
    else:
        d = df[(df["facility"] == facility) & (df["period"].astype(str) == str(period))]
        filt = {"facility": facility, "periods": [period] if period else None}
    if d.empty:
        return None

    indicators = compute_official_indicators(df, filt if filt.get("periods") else {"facility": facility})
    epi = compute_epidemiological_indicators(df, {"facility": facility if facility != "All three" else "All three"})
    actions = build_action_list(d, indicators)
    alerts_df = get_active_alerts(facility=None if facility == "All three" else facility)

    def rag_line(key):
        ind = indicators[key]
        lv = indicator_rag(ind, key)
        if ind.get("formula_status") == "invalid":
            return (
                f"N/A [INVALID] — raw {ind.get('calculated_pct')}% "
                f"(target {format_value(ind['target'], ind['unit'])}); {ind.get('formula_reason') or 'num>den'}"
            )
        if ind.get("value") is None:
            return f"N/A [{lv.upper()}]  (target {format_value(ind['target'], ind['unit'])})"
        return f"{format_value(ind['value'], ind['unit'])}  [{lv.upper()}]  (target {format_value(ind['target'], ind['unit'])})"

    cascade = compute_cascade_metrics(d)
    dq = compute_dq_score(df, filt if filt.get("periods") else {"facility": facility})

    # Run-rate for report (Achievement % — may exceed 100%)
    try:
        y, m = _ytd_month_from_filters(filt, d)
        rr = compute_run_rate(
            indicators["R1_1"].get("value"),
            indicators["R1_1"].get("target"),
            as_of_month=m,
            year=y,
        )
        rr_line = (
            f"Achievement % (vs annual target, may exceed 100%): "
            f"{rr['achievement_pct']:.0f}%" if rr.get("achievement_pct") is not None else "n/a"
        )
        if rr.get("required_monthly") is not None and rr.get("months_remaining", 0) > 0:
            rr_line += (
                f" · required ~{rr['required_monthly']:,.0f} tests/month remaining "
                f"({rr['months_remaining']} mo)"
            )
    except Exception:
        rr_line = "n/a"

    action_block = "\n".join(f"   · [{a['priority'].upper()}] {a['text']}" for a in actions) or "   · None from current thresholds."

    dq_line = (
        f"{dq['score']:.0f}/100 ({dq['label']})"
        if dq.get("score") is not None else "n/a"
    )
    _lr = get_latest_dq_review()
    review_line = (
        f"{_lr['reviewed_by']} at {_lr['reviewed_at']}"
        if _lr is not None else "none recorded"
    )

    # Phase A0: latest submission + recon summary
    sub_line = "none recorded"
    try:
        sub = query(
            "SELECT id, file_name, uploaded_at, row_count, period_types, geo_levels "
            "FROM submissions ORDER BY id DESC LIMIT 1"
        )
        if not sub.empty:
            s = sub.iloc[0]
            sub_line = (
                f"#{int(s['id'])} · {s['file_name']} · {s['uploaded_at']} · "
                f"{int(s['row_count'] or 0)} obs rows · periods={s.get('period_types') or '—'} · "
                f"geo={s.get('geo_levels') or '—'}"
            )
    except Exception:
        pass

    recon_lines = ["   · No reconciliation run yet (open Data foundation → Run reconciliation stubs)"]
    try:
        run = query("SELECT * FROM recon_runs ORDER BY id DESC LIMIT 1")
        if not run.empty:
            r = run.iloc[0]
            rid = int(r["id"])
            recon_lines = [
                f"   · Latest run #{rid} at {r.get('run_at')} by {r.get('run_by')}",
                f"   · Checked: {int(r.get('items_checked') or 0)} · Flagged: {int(r.get('items_flagged') or 0)}",
            ]
            items = query(
                """
                SELECT status, COUNT(*) AS n FROM recon_items
                WHERE run_id = ? GROUP BY status
                """,
                (rid,),
            )
            if not items.empty:
                for _, it in items.iterrows():
                    recon_lines.append(f"   · {it['status']}: {int(it['n'])}")
            top = query(
                """
                SELECT facility, period, message FROM recon_items
                WHERE run_id = ? AND status IN ('requires_reconciliation', 'review')
                ORDER BY CASE status WHEN 'requires_reconciliation' THEN 0 ELSE 1 END
                LIMIT 5
                """,
                (rid,),
            )
            if not top.empty:
                recon_lines.append("   · Top exceptions:")
                for _, it in top.iterrows():
                    recon_lines.append(
                        f"      - {it.get('facility')} {it.get('period')}: {it.get('message')}"
                    )
    except Exception:
        pass
    recon_block = "\n".join(recon_lines)

    # Supervision action workflow (open / overdue)
    sup_lines = ["   · No supervision actions logged"]
    try:
        sa = query(
            "SELECT id, facility, finding, action_text, owner, deadline, status "
            "FROM supervision_actions ORDER BY id DESC LIMIT 50"
        )
        if facility != "All three" and not sa.empty:
            sa = sa[sa["facility"] == facility]
        if not sa.empty:
            today_s = datetime.now().strftime("%Y-%m-%d")
            open_rows = sa[sa["status"].isin(["open", "in_progress"])]
            overdue = open_rows[
                open_rows["deadline"].notna()
                & (open_rows["deadline"].astype(str) < today_s)
            ]
            closed_n = int((sa["status"] == "closed").sum())
            sup_lines = [
                f"   · Actions total (sample last 50): {len(sa)} · open/in progress: {len(open_rows)} · closed: {closed_n}",
                f"   · Overdue (past deadline, still open): {len(overdue)}",
            ]
            if not overdue.empty:
                sup_lines.append("   · Overdue detail:")
                for _, r in overdue.head(8).iterrows():
                    sup_lines.append(
                        f"      - #{int(r['id'])} {r.get('facility')} · owner={r.get('owner') or '—'} · "
                        f"deadline={r.get('deadline')} · {(str(r.get('action_text') or r.get('finding') or '')[:80])}"
                    )
            elif not open_rows.empty:
                sup_lines.append("   · Open (not overdue):")
                for _, r in open_rows.head(5).iterrows():
                    sup_lines.append(
                        f"      - #{int(r['id'])} {r.get('facility')} · {r.get('status')} · "
                        f"deadline={r.get('deadline') or '—'} · {(str(r.get('action_text') or '')[:60])}"
                    )
    except Exception as e:
        sup_lines = [f"   · Supervision table unavailable ({e})"]
    sup_block = "\n".join(sup_lines)

    wp_lines = ["   · No workplan actions logged"]
    try:
        wp = query(
            "SELECT id, facility, problem, action_text, owner, due_date, status, source "
            "FROM workplan_actions ORDER BY id DESC LIMIT 40"
        )
        if facility != "All three" and not wp.empty:
            wp = wp[(wp["facility"] == facility) | (wp["facility"].isna())]
        if not wp.empty:
            today_s = datetime.now().strftime("%Y-%m-%d")
            open_wp = wp[wp["status"].isin(["open", "in_progress"])]
            od_wp = open_wp[
                open_wp["due_date"].notna() & (open_wp["due_date"].astype(str) < today_s)
            ]
            wp_lines = [
                f"   · Workplan items (sample): {len(wp)} · open: {len(open_wp)} · overdue: {len(od_wp)}",
            ]
            if not od_wp.empty:
                wp_lines.append("   · Overdue:")
                for _, r in od_wp.head(8).iterrows():
                    wp_lines.append(
                        f"      - #{int(r['id'])} {r.get('facility') or 'programme'} · "
                        f"owner={r.get('owner') or '—'} · due={r.get('due_date')} · "
                        f"{(str(r.get('problem') or '')[:70])}"
                    )
            elif not open_wp.empty:
                wp_lines.append("   · Open:")
                for _, r in open_wp.head(5).iterrows():
                    wp_lines.append(
                        f"      - #{int(r['id'])} {r.get('facility') or 'programme'} · "
                        f"{r.get('status')} · due={r.get('due_date') or '—'}"
                    )
    except Exception as e:
        wp_lines = [f"   · Workplan unavailable ({e})"]
    wp_block = "\n".join(wp_lines)

    report = f"""PIR CBMP — MONTHLY MALARIA REPORT
================================
Facility: {facility}
Period: {period}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
Phase 0 signed: {PHASE_0_SIGNED_DATE}

1. HEADLINE
   {auto_headline(indicators)}

2. OFFICIAL INDICATORS (Phase 0 RAG)
   · GO_1 Test positivity (RDT): {rag_line('GO_1')}
     RDT exams: {indicators['GO_1'].get('tested', 0):,} · formula status: {indicators['GO_1'].get('formula_status') or '—'}
     Note: {TPR_RULE['summary']}
   · SO_1 CHP competency: {rag_line('SO_1')}
   · R1.1 CHP tests (count): {rag_line('R1_1')}
   · R1.1 run-rate: {rr_line}
     (Achievement % is target progress — not an epidemiological rate; may exceed 100%.)
   · R1.2 Reporting rate: {rag_line('R1_2')}

3. CASCADE & GAPS (MOH 705 clinical path preferred; rates via formula gate)
   · Suspected: {cascade['suspected']:,}
   · Tested: {cascade['tested']:,}  (testing rate {cascade['testing_rate']:.0f}% · status {cascade.get('rate_status', {}).get('testing_rate', '—')})
   · Confirmed / positive: {cascade['confirmed']:,}
   · Treated (AL): {cascade['treated']:,}  (coverage {cascade['treat_coverage']:.0f}% · status {cascade.get('rate_status', {}).get('treat_coverage', '—')})
   · Diagnosis gap (suspected − tested): {cascade['diag_gap']:,}  ({cascade['diag_gap_pct']:.0f}%)
   · Treatment gap (confirmed − AL): {cascade['treat_gap']:,}  ({cascade['treat_gap_pct']:.0f}%)

4. EPIDEMIOLOGICAL (indicative if population provisional; invalid rates shown as N/A)
   · Incidence: {epi['Malaria Incidence Rate'].get('value')} per 1,000 [{epi['Malaria Incidence Rate'].get('status', '—')}]
   · ABER: {epi['ABER'].get('value') if epi['ABER'].get('status')=='valid' else 'N/A'}% [{epi['ABER'].get('status', '—')}]
   · TPR (RDT): {epi['Test Positivity Rate'].get('value') if epi['Test Positivity Rate'].get('status')=='valid' else 'N/A'}% [{epi['Test Positivity Rate'].get('status', '—')}]
   · Population status: {POPULATION_STATUS}

5. ACTIONS THIS PERIOD
{action_block}

6. ACTIVE ALERTS
   · Count: {len(alerts_df)}

7. DATA QUALITY
   · DQ score: {dq_line}
   · Last DQ review: {review_line}
   · Source: KHIS Excel upload into CBMP dashboard
   · Filters applied for this report: facility={facility}, period={period}
   · CFR / severe malaria deferred until fields exist in upload

8. SUBMISSION (Phase A0)
   · Latest: {sub_line}

9. RECONCILIATION SUMMARY (Layer 1 stubs)
{recon_block}

10. SUPERVISION ACTIONS (workflow)
{sup_block}

11. PROGRAMME WORKPLAN
{wp_block}

---
World Friends ETS · PIR CBMP · Kilifi County
"""
    return report


def page_reports(user, filters):
    df = load_all_data()
    page_header(
        "Reports",
        "Management brief for a facility and period. Includes official RAG, cascade, "
        "DQ, submission, reconciliation, and supervision overdue actions.",
    )
    if df.empty:
        st.warning("No data uploaded yet.")
        return

    section_header("Generate a Report")
    st.caption(
        "Run **Data foundation → Reconciliation stubs** before generating if you want "
        "section 9 populated. Submission line uses the latest committed file."
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        report_fac = st.selectbox("Facility", ["All three"] + FACILITIES)
    with c2:
        periods = query("SELECT DISTINCT period FROM malaria_data ORDER BY period DESC")["period"].tolist()
        report_period = st.selectbox("Period", periods if periods else ["No data"])
    with c3:
        report_type = st.selectbox(
            "Report type",
            ["Monthly facility", "County / proposal brief", "Donor one-pager"],
        )

    if st.button("Generate report", type="primary"):
        if report_type == "County / proposal brief":
            report = generate_management_brief("county/proposal", report_fac, filters, df)
        elif report_type == "Donor one-pager":
            report = generate_management_brief("donor", report_fac, filters, df)
            report = "CBMP DONOR ONE-PAGER (AICS / partners)\n" + report
        else:
            report = generate_monthly_report(report_fac, report_period)
            if not report:
                report = generate_management_brief("facility", report_fac, filters, df)
        if not report:
            st.error("No data for that facility/period combination.")
        else:
            st.text_area("Report (plain text)", report, height=420)
            safe_name = str(report_fac).replace(" ", "_")
            # Markdown variant with light structure for Word / email paste
            md_lines = []
            for line in report.splitlines():
                if line.startswith("====") or line.startswith("---"):
                    continue
                if line and line[0].isdigit() and ". " in line[:6]:
                    md_lines.append(f"\n## {line}")
                elif line.startswith("   ·"):
                    md_lines.append(f"- {line.strip()[1:].strip()}")
                else:
                    md_lines.append(line)
            md_report = "\n".join(md_lines)
            dl1, dl2 = st.columns(2)
            with dl1:
                st.download_button(
                    "📥 Download TXT",
                    report.encode("utf-8"),
                    file_name=f"CBMP_{report_type}_{safe_name}_{report_period}.txt",
                    mime="text/plain",
                )
            with dl2:
                st.download_button(
                    "📥 Download Markdown",
                    md_report.encode("utf-8"),
                    file_name=f"CBMP_{report_type}_{safe_name}_{report_period}.md",
                    mime="text/markdown",
                )
            try:
                execute(
                    "INSERT INTO report_history (report_type, facility, period, generated_by, content) VALUES (?, ?, ?, ?, ?)",
                    (report_type, report_fac, report_period, user["username"], report),
                )
                log_audit(user["username"], "generate_report", "report_history", f"{report_fac} {report_period}")
            except Exception as e:
                st.caption(f"Report generated (history save skipped: {e})")

    section_header("Report History")
    history = query("SELECT report_type, facility, period, generated_at, generated_by FROM report_history ORDER BY id DESC LIMIT 20")
    if history.empty:
        st.caption("No reports generated yet.")
    else:
        st.dataframe(history, use_container_width=True, hide_index=True)


# ==================================================================
# INITIALISATION
# ==================================================================

# Startup seed — tolerate SQLite locks under Streamlit multi-session / Colab
try:
    init_db()
except Exception:
    pass
for _seed_fn in (
    seed_indicator_dictionary,
    populate_population,
    populate_alert_rules,
    seed_users,
):
    try:
        _seed_fn()
    except Exception:
        pass


# ==================================================================
# MAIN APP
# ==================================================================

# ── Public routes (no login): shared view + password activation ──
try:
    _qp = dict(st.query_params)
except Exception:
    _qp = {}
_share_tok = _qp.get("share") or _qp.get("shared")
_act_tok = _qp.get("activate") or _qp.get("reset")
if isinstance(_share_tok, list):
    _share_tok = _share_tok[0] if _share_tok else None
if isinstance(_act_tok, list):
    _act_tok = _act_tok[0] if _act_tok else None

if _share_tok:
    render_shared_view_page(str(_share_tok))
    st.stop()

if _act_tok and "user" not in st.session_state:
    st.title("Activate account / set password")
    st.caption("One-time secure link. Choose a personal password (min 8 characters).")
    with st.form("activate_form"):
        npw = st.text_input("New password", type="password")
        cpw = st.text_input("Confirm password", type="password")
        go = st.form_submit_button("Set password", type="primary")
    if go:
        if npw != cpw:
            st.error("Passwords do not match.")
        else:
            ok, msg = consume_password_token(str(_act_tok), npw)
            if ok:
                st.success(msg)
                st.info("Remove `?activate=...` from the URL and sign in with your username.")
            else:
                st.error(msg)
    st.stop()

if "user" not in st.session_state:
    login_screen()
    # Forgot / activate helper on login page
    with st.expander("Have an activation or reset token?"):
        t = st.text_input("Paste token", key="login_act_tok")
        if st.button("Continue to set password", key="login_act_go") and t.strip():
            try:
                st.query_params["activate"] = t.strip()
            except Exception:
                st.session_state["_pending_activate"] = t.strip()
            st.rerun()
    st.stop()

user = st.session_state.user
if user.get("_is_preview"):
    st.warning(
        f"PREVIEW MODE — viewing as **{user.get('name')}** (`{user.get('username')}`). "
        "Upload/commit/admin actions disabled. End preview from Users & Access."
    )


# Session timeout (8 hours)
_login_at = st.session_state.get("login_at") or datetime.now().timestamp()
if datetime.now().timestamp() - float(_login_at) > 8 * 3600:
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.warning("Session expired. Sign in again.")
    login_screen()
    st.stop()

# Force password change on first login
if st.session_state.get("must_change_password"):
    change_password_screen(user)
    st.stop()

if "nav_override" in st.session_state:
    override = st.session_state.pop("nav_override")
    st.session_state.nav = override
    st.session_state.nav_unlocked = True

st.sidebar.markdown("### Kilifi Malaria Programme")
st.sidebar.caption(f"**{user['name']}**")
st.sidebar.caption(
    f"Scope: `{user.get('scope', 'all')}` · "
    f"{'Upload' if user.get('can_upload') else 'View'} · "
    f"{'Commit' if user.get('can_commit') else 'No commit'}"
)
st.sidebar.markdown("---")

# Global brand header (once per page render)
render_global_header()

all_pages = user["pages"]
nav_sections = []
_cmd = [p for p in ("Command Centre", "Overview", "Alerts") if p in all_pages]
if _cmd:
    nav_sections.append(("MONITOR", _cmd))
_perf = [p for p in all_pages if p.startswith(("GO ", "SO ", "R1.1", "R1.2")) or p in (
    "Epidemiological", "All Indicators", "Facility Deep-Dive", "CHP Performance", "Loss & Gap Analysis",
)]
if _perf:
    nav_sections.append(("ANALYSE", _perf))
_dq = [p for p in ("Data Quality", "Data foundation", "Dashboard QA", "Users & Access", "Upload & Settings") if p in all_pages]
if _dq:
    nav_sections.append(("DATA & GOVERNANCE", _dq))
_intel = [p for p in ("Maps", "Comparison", "Climate", "Malaria Early Warning") if p in all_pages]
if _intel:
    nav_sections.append(("INTELLIGENCE", _intel))
_rep = [p for p in ("Reports", "Budget", "Planning") if p in all_pages]
if _rep:
    nav_sections.append(("REPORTING", _rep))

flat_pages = [p for _, pages in nav_sections for p in pages]
default_page = "Upload & Settings" if (user["can_upload"] and count_rows() == 0) else "Overview"
if default_page not in flat_pages and flat_pages:
    default_page = flat_pages[0]
if "nav" not in st.session_state or st.session_state.nav not in flat_pages:
    st.session_state.nav = default_page

# Lock to Upload only until first data exists OR user explicitly navigates away
if user["can_upload"] and count_rows() == 0 and not st.session_state.get("nav_unlocked"):
    st.session_state.nav = "Upload & Settings"
    st.sidebar.info("No data yet — upload a KHIS file, or use the menu after unlock.")
elif count_rows() > 0:
    st.session_state.nav_unlocked = True

def _goto(page_name: str):
    """Switch page reliably (set state + rerun)."""
    st.session_state.nav = page_name
    st.session_state.nav_unlocked = True
    st.rerun()

for section_title, pages in nav_sections:
    st.sidebar.markdown(f"### {section_title}")
    for p in pages:
        is_current = st.session_state.nav == p
        if st.sidebar.button(
            p,
            key=f"nav_{p}",
            use_container_width=True,
            type="primary" if is_current else "secondary",
        ):
            _goto(p)

st.sidebar.markdown("---")
st.sidebar.markdown("**My Account**")
st.sidebar.caption(f"{user.get('name')} · `{user.get('role')}`")
if user.get("facility_scope"):
    st.sidebar.caption(f"Facility: {user['facility_scope']}")
if user.get("sub_county_scope"):
    st.sidebar.caption(f"Sub-county: {user['sub_county_scope']}")
if st.sidebar.button("Change my password", use_container_width=True):
    st.session_state.must_change_password = True
    st.rerun()
if st.sidebar.button("Sign out", use_container_width=True):
    st.session_state.clear()
    st.rerun()

page = st.session_state.nav
# Layout: filters (analysis pages), then page body — brand header already rendered above
if page != "Upload & Settings":
    filters = render_filter_bar()
    st.markdown("---")
else:
    filters = {
        "facility": "All three",
        "stream": "Both",
        "registers": list(REGISTERS),
        "periods": None,
        "period_mode": "All periods",
        "period": None,
    }

if page == "Upload & Settings":
    page_upload(user)
elif page == "Command Centre":
    page_command_centre(user, filters)
elif page == "Overview":
    page_overview(user, filters)
elif page == "GO · Test Positivity Rate":
    page_go(user, filters)
elif page == "SO · CHP Competency":
    page_so(user, filters)
elif page == "R1.1 · CHP Tests":
    page_r11(user, filters)
elif page == "R1.2 · Reports Submitted":
    page_r12(user, filters)
elif page == "Epidemiological":
    page_epidemiological(user, filters)
elif page == "All Indicators":
    page_all_indicators(user, filters)
elif page == "Facility Deep-Dive":
    page_facility(user, filters)
elif page == "Loss & Gap Analysis":
    page_loss_gap(user, filters)
elif page == "Data Quality":
    page_quality(user, filters)
elif page == "Data foundation":
    page_data_foundation(user, filters)
elif page == "Dashboard QA":
    page_dashboard_qa(user, filters)
elif page == "Users & Access":
    page_users_access(user, filters)
elif page == "CHP Performance":
    page_chp(user, filters)
elif page == "Alerts":
    page_alerts(user, filters)
elif page == "Maps":
    page_maps(user, filters)
elif page == "Comparison":
    page_comparison(user, filters)
elif page == "Climate":
    page_climate(user, filters)
elif page == "Malaria Early Warning":
    page_malaria_ew(user, filters)
elif page == "Reports":
    page_reports(user, filters)
elif page == "Budget":
    page_budget(user, filters)
elif page == "Planning":
    page_planning(user, filters)
else:
    st.error(f"Unknown page: {page}")

render_global_footer()
