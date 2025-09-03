import os
import streamlit as st
from langchain_ollama import ChatOllama
from dotenv import load_dotenv
import msal
import requests
import datetime
from urllib.parse import urlencode
from dateutil.relativedelta import relativedelta
import re

# ==============================
# 1) Load Config
# ==============================

load_dotenv()
TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
USER_ID = os.getenv("AZURE_USER_ID")
OUTLOOK_TZ = os.getenv("OUTLOOK_TIMEZONE", "Asia/Kolkata")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_TEMP = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read", "Calendars.Read"]  # delegated

# ==============================
# 2) Auth for Microsoft Graph
# ==============================
def get_delegated_token():
    app = msal.PublicClientApplication(client_id=CLIENT_ID, authority=AUTHORITY)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError("Failed to initiate device flow.")

    st.warning(f"Please authenticate: {flow['verification_uri']} with code {flow['user_code']}")
    result = app.acquire_token_by_device_flow(flow)  # blocks
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description"))
    return result["access_token"]

def get_app_token():
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default"
    }
    resp = requests.post(url, data=data)
    resp.raise_for_status()
    return resp.json()["access_token"]

# ==============================
# 3) Fetch Calendar Events
# ==============================

def parse_date_range(user_text: str):
    """
    Parse human-like date ranges from user text.
    Returns: (start_date, end_date, period_desc)
    """

    now = datetime.datetime.utcnow()

    # Default = last 3 months
    start = now - relativedelta(months=3)
    end = now
    desc = "last 3 months"

    # Match weeks
    week_match = re.search(r"last\s+(\d+)\s+weeks?", user_text, re.I)
    if week_match:
        n = int(week_match.group(1))
        start = now - datetime.timedelta(weeks=n)
        desc = f"last {n} week(s)"

    # Match months
    month_match = re.search(r"last\s+(\d+)\s+months?", user_text, re.I)
    if month_match:
        n = int(month_match.group(1))
        start = now - relativedelta(months=n)
        desc = f"last {n} month(s)"

    # Match days
    day_match = re.search(r"last\s+(\d+)\s+days?", user_text, re.I)
    if day_match:
        n = int(day_match.group(1))
        start = now - datetime.timedelta(days=n)
        desc = f"last {n} day(s)"

    # "next month"
    if re.search(r"next\s+month", user_text, re.I):
        start = now
        end = now + relativedelta(months=1)
        desc = "next month"

    # "next week"
    if re.search(r"next\s+week", user_text, re.I):
        start = now
        end = now + datetime.timedelta(weeks=1)
        desc = "next week"

    return start, end, desc

def iso_utc(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"

def fetch_user_events(user_id, token, start_date, end_date):

    url = (
        f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView"
        f"?startDateTime={start_date.isoformat()}Z&endDateTime={end_date.isoformat()}Z"
        f"&$select=subject,start,end,location&$orderby=start/dateTime"
    )
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json().get("value", [])

def shape_events(events):
    rows = []
    for e in events:
        rows.append({
            "subject": e.get("subject", "(no subject)"),
            "start": e.get("start", {}).get("dateTime"),
            "end": e.get("end", {}).get("dateTime"),
            "location": e.get("location", {}).get("displayName", ""),
            "all_day": e.get("isAllDay", False)
        })
    return rows

def events_to_text(events):
    lines = []
    for e in events:
        when = f"{e['start']} → {e['end']}" if e.get('start') else ""
        where = f" @ {e['location']}" if e['location'] else ""
        flag = " (ALL-DAY)" if e['all_day'] else ""
        lines.append(f"- {e['subject']}{flag} | {when}{where}")
    return "\n".join(lines)

# ==============================
# 4) LLM Summarization
# ==============================
def summarize_events(events, period_desc="last 3 months"):
    if not events:
        return f"No events found in {period_desc}."

    text_block = events_to_text(events)
    system = "You are a sharp executive assistant. Be concise and structured."
    prompt = f"""
Here are calendar events from {period_desc}:

{text_block}

Summarize with:
"""
    llm = ChatOllama(model=OLLAMA_MODEL, base_url="http://localhost:11434", temperature=OLLAMA_TEMP)
    resp = llm.invoke([{"role": "system", "content": system},
                       {"role": "user", "content": prompt}])
    return resp.content


# ==============================
# 5) Streamlit UI
# ==============================
st.title("📅 Calendar Summarizer (Ollama + Graph)")

with st.form("llm_form"):
    text = st.text_area("Enter Your Prompt (e.g., 'Summarize last 3 months of meetings')")
    submit = st.form_submit_button("Submit")

if "chat_history" not in st.session_state:
    st.session_state['chat_history'] = []

if submit and text:
    with st.spinner("Fetching and summarizing..."):
        token = get_app_token()

        # 🔥 Parse user’s request for dynamic period
        start_date, end_date, period_desc = parse_date_range(text)

        # Fetch events dynamically
        events = fetch_user_events(USER_ID, token, start_date, end_date)
        shaped = shape_events(events)
        summary = summarize_events(shaped, period_desc)
        st.session_state['chat_history'].append({"user": text, "ollama": summary})
        st.write(summary)

st.write("## Chat History")
for chat in reversed(st.session_state['chat_history']):
    st.write(f"**🧑 User**: {chat['user']}")
    st.write(f"**🧠 Assistant**: {chat['ollama']}")
    st.write("---")


# st.title("Hello")
# with st.form("llm_form"):
#     text = st.text_area("Enter Your Text")
#     submit = st.form_submit_button("Submit")

# def generate_response(input_text):
#     model = ChatOllama(model="llama3.2", base_url="http://localhost:11434/")

#     response = model.invoke(input_text)

#     return response.content

# if "chat_history" not in st.session_state:
#     st.session_state['chat_history'] = []

# if submit and text:
#     with st.spinner("Generating response..."):
#         response = generate_response(text)
#         st.session_state['chat_history'].append({"user": text, "ollama": response})
#         st.write(response)

# st.write("## Chat History")
# for chat in reversed(st.session_state['chat_history']):
#     st.write(f"**🧑 User**: {chat['user']}")
#     st.write(f"**🧠 Assistant**: {chat['ollama']}")
#     st.write("---")