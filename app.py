"""
uKids Directors Scheduler
--------------------------
Reads:
  1) Availability sheet  -> Group, Name, then one column per date+session
                             e.g. "2 August - Morning", "2 August - Evening"
                             Values: "Yes" / "No" / anything else = not available
  2) Rotation sheet       -> Position (rows) x Month-Session (columns)
                             e.g. "Aug-M", "Aug-E", "Sept-M" ...
                             Values: a Group letter (A-E) = who covers that
                             position that month/session

Produces a shuffled, fair schedule: for every date + session, for every
position that has a group assigned that month, pick an available person
from that group -- favoring whoever has served the least so far.
"""

import re
import random
from collections import defaultdict

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="uKids Directors Scheduler", layout="wide")

# ---------------------------------------------------------------------------
# Google Sheets connection
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


@st.cache_resource
def get_gspread_client():
    creds_dict = st.secrets["gcp_service_account"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def load_sheet_as_df(sheet_id: str, worksheet_name: str) -> pd.DataFrame:
    client = get_gspread_client()
    sh = client.open_by_key(sheet_id)
    ws = sh.worksheet(worksheet_name)
    records = ws.get_all_records()
    return pd.DataFrame(records)


def write_df_to_sheet(sheet_id: str, worksheet_name: str, df: pd.DataFrame):
    client = get_gspread_client()
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(worksheet_name)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=200, cols=20)
    ws.update([df.columns.values.tolist()] + df.values.tolist())


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

MONTH_ABBR = {
    "January": "Jan", "February": "Feb", "March": "Mar", "April": "Apr",
    "May": "May", "June": "June", "July": "July", "August": "Aug",
    "September": "Sept", "October": "Oct", "November": "Nov", "December": "Dec",
}

DATE_COL_PATTERN = re.compile(r"^\s*(\d{1,2}\s+[A-Za-z]+)\s*-\s*(Morning|Evening)\s*$")


def find_date_session_columns(availability_df: pd.DataFrame):
    """Return list of (column_name, date_label, month_name, session) for every
    date+session column found in the availability sheet."""
    found = []
    for col in availability_df.columns:
        m = DATE_COL_PATTERN.match(str(col))
        if not m:
            continue
        date_label, session = m.group(1), m.group(2)
        month_name = date_label.split()[-1]
        found.append((col, date_label, month_name, session))
    return found


def rotation_column_for(month_name: str, session: str):
    abbr = MONTH_ABBR.get(month_name, month_name[:3])
    code = "M" if session == "Morning" else "E"
    return f"{abbr}-{code}"


def build_roster(availability_df: pd.DataFrame):
    """Group letter -> list of names."""
    roster = defaultdict(list)
    for _, row in availability_df.iterrows():
        group = str(row.get("Group", "")).strip()
        name = str(row.get(row.index[1], "")).strip()  # 2nd column = name
        # More robust: use explicit column names if present
        name_col = "Serving Girl" if "Serving Girl" in availability_df.columns else availability_df.columns[1]
        name = str(row.get(name_col, "")).strip()
        if group and name:
            roster[group].append(name)
    return roster


# ---------------------------------------------------------------------------
# Core scheduling logic
# ---------------------------------------------------------------------------

def generate_schedule(availability_df: pd.DataFrame, rotation_df: pd.DataFrame, seed: int = None):
    if seed is not None:
        random.seed(seed)

    roster = build_roster(availability_df)
    date_session_cols = find_date_session_columns(availability_df)

    # Position column in rotation sheet may be named "Position" or unnamed first col
    position_col = rotation_df.columns[0]

    # Track how many times each person has already been assigned, to spread load fairly
    load_count = defaultdict(int)

    rows_out = []
    warnings = []

    for col, date_label, month_name, session in date_session_cols:
        rot_col = rotation_column_for(month_name, session)
        if rot_col not in rotation_df.columns:
            warnings.append(f"No rotation column found for {date_label} ({session}) — expected '{rot_col}'.")
            continue

        already_used_today = set()

        for _, rot_row in rotation_df.iterrows():
            position = str(rot_row[position_col]).strip()
            group = str(rot_row.get(rot_col, "")).strip()
            if not position or not group:
                continue

            group_members = roster.get(group, [])

            # Who in this group said Yes for this date+session, and isn't already
            # doing another position today?
            candidates = []
            for _, person_row in availability_df.iterrows():
                name_col = "Serving Girl" if "Serving Girl" in availability_df.columns else availability_df.columns[1]
                person_name = str(person_row.get(name_col, "")).strip()
                person_group = str(person_row.get("Group", "")).strip()
                if person_group != group or person_name not in group_members:
                    continue
                if person_name in already_used_today:
                    continue
                if str(person_row.get(col, "")).strip().lower() == "yes":
                    candidates.append(person_name)

            if not candidates:
                rows_out.append({
                    "Date": date_label, "Session": session, "Position": position,
                    "Group": group, "Assigned": "⚠ NEEDS COVERAGE",
                })
                warnings.append(f"No one available for {position} ({group}) on {date_label} {session}.")
                continue

            # Fairness: pick whoever has served the fewest times so far;
            # break ties randomly so it's not always the same person.
            random.shuffle(candidates)
            chosen = min(candidates, key=lambda n: load_count[n])

            load_count[chosen] += 1
            already_used_today.add(chosen)

            rows_out.append({
                "Date": date_label, "Session": session, "Position": position,
                "Group": group, "Assigned": chosen,
            })

    schedule_df = pd.DataFrame(rows_out)
    return schedule_df, warnings


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.title("🗓️ uKids Directors Scheduler")
st.caption("Shuffles director availability against the group/position rotation to build a fair monthly schedule.")

with st.sidebar:
    st.header("Google Sheet settings")
    sheet_id = st.text_input("Google Sheet ID", help="The long ID from the sheet's URL")
    avail_ws = st.text_input("Availability tab name", value="Responses")
    rotation_ws = st.text_input("Rotation tab name", value="Rotation")
    output_ws = st.text_input("Output tab name (for writing back)", value="Draft Schedule")
    load_btn = st.button("Load data", use_container_width=True)

if "availability_df" not in st.session_state:
    st.session_state.availability_df = None
    st.session_state.rotation_df = None

if load_btn and sheet_id:
    try:
        st.session_state.availability_df = load_sheet_as_df(sheet_id, avail_ws)
        st.session_state.rotation_df = load_sheet_as_df(sheet_id, rotation_ws)
        st.success("Data loaded.")
    except Exception as e:
        st.error(f"Couldn't load the sheet: {e}")

# Fallback: manual upload, in case Sheets access isn't set up yet
st.subheader("Or upload files directly")
col1, col2 = st.columns(2)
with col1:
    avail_file = st.file_uploader("Availability (.csv or .xlsx)", type=["csv", "xlsx"])
    if avail_file:
        st.session_state.availability_df = (
            pd.read_csv(avail_file) if avail_file.name.endswith("csv") else pd.read_excel(avail_file)
        )
with col2:
    rotation_file = st.file_uploader("Rotation table (.csv or .xlsx)", type=["csv", "xlsx"])
    if rotation_file:
        st.session_state.rotation_df = (
            pd.read_csv(rotation_file) if rotation_file.name.endswith("csv") else pd.read_excel(rotation_file)
        )

if st.session_state.availability_df is not None and st.session_state.rotation_df is not None:
    st.divider()
    st.subheader("Preview")
    with st.expander("Availability data"):
        st.dataframe(st.session_state.availability_df, use_container_width=True)
    with st.expander("Rotation table"):
        st.dataframe(st.session_state.rotation_df, use_container_width=True)

    st.divider()
    shuffle_seed = st.session_state.get("shuffle_seed", 0)
    if st.button("🔀 Shuffle & generate schedule", type="primary"):
        st.session_state.shuffle_seed = random.randint(0, 999999)
        st.rerun()

    if "shuffle_seed" in st.session_state:
        schedule_df, warnings = generate_schedule(
            st.session_state.availability_df,
            st.session_state.rotation_df,
            seed=st.session_state.shuffle_seed,
        )

        if warnings:
            st.warning("Some spots need attention:\n\n" + "\n".join(f"- {w}" for w in warnings))

        morning_df = schedule_df[schedule_df["Session"] == "Morning"].reset_index(drop=True)
        evening_df = schedule_df[schedule_df["Session"] == "Evening"].reset_index(drop=True)

        st.subheader("☀️ Morning schedule")
        st.dataframe(morning_df, use_container_width=True)

        st.subheader("🌙 Evening schedule")
        st.dataframe(evening_df, use_container_width=True)

        csv_data = schedule_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download schedule (CSV)", csv_data, "directors_schedule.csv", "text/csv")

        if sheet_id and st.button("Write schedule back to Google Sheet"):
            try:
                write_df_to_sheet(sheet_id, output_ws, schedule_df)
                st.success(f"Written to '{output_ws}' tab.")
            except Exception as e:
                st.error(f"Couldn't write to the sheet: {e}")
else:
    st.info("Load your Google Sheet or upload both files above to get started.")
