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
import subprocess
import json
from tzlocal import get_localzone

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
# 3) Parse Date Range
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


def extract_date_range_with_ollama(user_prompt: str):
    """
    Uses Ollama (llama3 or similar) to extract a start and end date from the user's natural prompt.
    """
    system_prompt = f"""
You are a date extraction assistant.
The current date is {datetime.date.today()}.
Extract the start_date and end_date from the user prompt below.
If the prompt is vague like "next week" or "this month", infer the exact date range.
Return the result ONLY in strict JSON with keys: start_date, end_date, description.
Example output:
{{"start_date": "2025-10-01", "end_date": "2025-10-07", "description": "Next week"}}
User prompt: "{user_prompt}"
"""

    process = subprocess.Popen(
        ["ollama", "run", "llama3"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8'
    )

    output, _ = process.communicate(system_prompt)

    # Try to find a JSON object using regex
    try:
        match = re.search(r"\{[\s\S]*\}", output)
        if not match:
            raise ValueError("No JSON object found in output.")

        json_str = match.group(0).strip()

        # Attempt to load JSON
        data = json.loads(json_str)

        # Basic validation
        for key in ["start_date", "end_date", "description"]:
            if key not in data:
                raise ValueError(f"Missing key: {key}")

        return data

    except Exception as e:
        return {
            "error": f"Could not parse dates: {e}",
            "raw_output": output.strip()
        }

# def handle_date_extraction(date_info):
#     if "error" in date_info:
#         return f"❌ {date_info['error']}\n\nRaw Output: {date_info['raw_output']}"
#     return date_info        

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

def create_event(user_id, token, subject, start_time, end_time, location=None):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "subject": subject,
        "start": {
            "dateTime": start_time,
            "timeZone": "UTC"
        },
        "end": {
            "dateTime": end_time,
            "timeZone": "UTC"
        },
    }
    if location:
        data["location"] = {"displayName": location}

    resp = requests.post(url, headers=headers, json=data)
    resp.raise_for_status()
    return resp.json()

def search_event(user_id, token, subject):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events?$filter=startswith(subject,'{subject}')"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        events = response.json().get('value', [])
        return events
    else:
        print("Search failed:", response.text)
        return []

def update_event(user_id, token, event_id, subject=None, start_time=None, end_time=None, location=None):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    update_data = {}
    if subject:
        update_data["subject"] = subject
    if start_time:
        update_data["start"] = {"dateTime": start_time, "timeZone": "UTC"}
    if end_time:
        update_data["end"] = {"dateTime": end_time, "timeZone": "UTC"}
    if location:
        update_data["location"] = {"displayName": location}

    resp = requests.patch(url, headers=headers, json=update_data)
    resp.raise_for_status()
    return resp.json()


def delete_event(user_id, token, event_id):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(url, headers=headers)
    if resp.status_code == 204:
        return {"message": "Event deleted successfully"}
    else:
        return {"error": f"Failed to delete: {resp.text}"}

# ==============================
# 4) Analyze User Intent
# ==============================

def analyze_user_intent_with_ollama(user_prompt: str):
    """
    Uses Ollama (llama3) to determine user's calendar intent (show/create/update/delete)
    and extract structured event details.

    Improvements:
    ✅ Handles both JSON and markdown-style responses
    ✅ Cleans invalid comments
    ✅ Auto-adds +30 min end_time if missing
    ✅ Converts UTC → system timezone
    """

    system_prompt = f"""
You are a Microsoft Calendar AI assistant. Today's date is {datetime.date.today()}.
Understand what the user wants (show/create/update/delete), even if they use synonyms.

🔹 Intent mapping rules:
- "show" → includes: show, list, display, fetch, get, view, summarize
- "create" → includes: create, add, schedule, book, make, plan, set up
- "update" → includes: update, change, edit, modify, move, reschedule, postpone
- "delete" → includes: delete, remove, cancel

Return only valid JSON with these keys:
- intent: one of "show" | "create" | "update" | "delete"
- subject: event name
- date: YYYY-MM-DD if specified
- start_time: HH:MM (24h)
- end_time: HH:MM (optional)
- location: if mentioned
- notes: extra context

Now analyze this user request:
"{user_prompt}"
"""

    process = subprocess.Popen(
        ["ollama", "run", "llama3"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )
    output, _ = process.communicate(system_prompt)

    try:
        cleaned = output.strip()

        # --- Clean up markdown / comments ---
        cleaned = re.sub(r"```(?:json)?", "", cleaned)
        cleaned = cleaned.replace("```", "")
        cleaned = re.sub(r"//.*", "", cleaned)
        cleaned = re.sub(r"#.*", "", cleaned)

        # --- Try to find JSON first ---
        matches = list(re.finditer(r"\{[\s\S]*?\}", cleaned))
        result = None

        if matches:
            json_str = matches[-1].group(0).strip()
            result = json.loads(json_str)

        else:
            # --- No JSON: Try to parse markdown key-value pairs ---
            kv_pattern = re.findall(r"\*\*([\w_]+)\*\*:\s*([^\n\r]+)", cleaned)
            if kv_pattern:
                result = {k.lower(): v.strip().strip('"') for k, v in kv_pattern}
            else:
                raise ValueError("No JSON or key-value pairs found in Ollama output.")

        # --- Normalize missing fields ---
        for key in ["intent", "subject", "date", "start_time", "end_time", "location", "notes"]:
            result.setdefault(key, "")

        # --- Add +30 mins if end_time missing ---
        start_time = result.get("start_time")
        end_time = result.get("end_time")
        if start_time and not end_time:
            try:
                t = datetime.datetime.strptime(start_time, "%H:%M")
                end_t = (t + datetime.timedelta(minutes=30)).time()
                result["end_time"] = end_t.strftime("%H:%M")
            except ValueError:
                pass

        # --- Convert UTC → local timezone ---
        if start_time:
            try:
                local_tz = get_localzone()
                utc = pytz.utc
                t = datetime.datetime.strptime(start_time, "%H:%M")
                t_utc = utc.localize(t)
                t_local = t_utc.astimezone(local_tz)
                result["start_time"] = t_local.strftime("%H:%M")

                if result.get("end_time"):
                    e = datetime.datetime.strptime(result["end_time"], "%H:%M")
                    e_utc = utc.localize(e)
                    e_local = e_utc.astimezone(local_tz)
                    result["end_time"] = e_local.strftime("%H:%M")
            except Exception:
                pass

        return result

    except Exception as e:
        return {
            "error": f"Intent parsing failed: {e}",
            "raw": output
        }

# ==============================
# 5) Streamlit UI
# ==============================
st.title("📅 Calendar Assistance (Ollama + Graph)")

with st.form("llm_form"):
    text = st.text_area("Enter Your Prompt (e.g., 'Summarize last 3 months of meetings')")
    submit = st.form_submit_button("Submit")

if "chat_history" not in st.session_state:
    st.session_state['chat_history'] = []

# if submit and text:
#     with st.spinner("Fetching and summarizing..."):
#         token = get_app_token()
        
#         # 🔥 Parse user’s request for dynamic period
#         date_info = extract_date_range_with_ollama(text)

        
#         if "error" in date_info:
#             st.write(f"❌ {date_info['error']}\nRaw Output:\n{date_info['raw_output']}")
#         else:
#             start_date = datetime.datetime.fromisoformat(date_info["start_date"])
#             end_date = datetime.datetime.fromisoformat(date_info["end_date"])
#             period_desc = date_info["description"]

#             # Fetch events dynamically
#             events = fetch_user_events(USER_ID, token, start_date, end_date)
#             shaped = shape_events(events)
#             summary = summarize_events(shaped, period_desc)
#             st.session_state['chat_history'].append({"user": text, "ollama": summary})
#             st.write(summary)

if submit and text:
    with st.spinner("🧠 Ollama analyzing your request..."):
        token = get_app_token()
        analysis = analyze_user_intent_with_ollama(text)

        user_msg = {"user": text}
        ollama_reply = ""

        if "error" in analysis:
            error_msg = f"❌ {analysis['error']}"
            st.error(error_msg)
            raw_output = analysis.get("raw", "")
            if raw_output:
                st.text(raw_output)
            ollama_reply = error_msg + "\n" + raw_output
            st.session_state['chat_history'].append({"user": text, "ollama": ollama_reply})
        else:
            intent = analysis.get("intent", "").lower()
            subject = analysis.get("subject", "")
            date_str = analysis.get("date", "")
            location = analysis.get("location", "")
            start_time = analysis.get("start_time", "")
            end_time = analysis.get("end_time", "")
            notes = analysis.get("notes", "")

            # Convert parsed date/time
            start_dt = end_dt = None
            if date_str:
                try:
                    base = datetime.datetime.fromisoformat(date_str)
                    if start_time:
                        h, m = map(int, start_time.split(":"))
                        start_dt = base.replace(hour=h, minute=m)
                    if end_time:
                        h, m = map(int, end_time.split(":"))
                        end_dt = base.replace(hour=h, minute=m)
                except Exception:
                    pass

            try:
                if intent == "show":
                    start, end, desc = parse_date_range(notes or text)
                    events = fetch_user_events(USER_ID, token, start, end)
                    shaped = shape_events(events)
                    summary = summarize_events(shaped, desc)
                    ollama_reply = summary
                    st.write(summary)

                elif intent == "create":
                    if not (subject and start_dt and end_dt):
                        ollama_reply = "⚠️ Missing event details (subject or time)."
                        st.error(ollama_reply)
                    else:
                        created = create_event(USER_ID, token, subject, start_dt.isoformat(), end_dt.isoformat(), location)
                        ollama_reply = f"✅ Created: {subject} ({created.get('id')})"
                        st.success(ollama_reply)

                elif intent == "update":
                    if not subject:
                        ollama_reply = "⚠️ Please specify the event subject to update."
                        st.error(ollama_reply)
                    else:
                        events = search_event(USER_ID, token, subject)
                        if not events:
                            ollama_reply = f"⚠️ No event found with subject '{subject}'"
                            st.warning(ollama_reply)
                        else:
                            event_id = events[0]['id']
                            updated = update_event(USER_ID, token, event_id, subject, start_dt.isoformat(), end_dt.isoformat(), location)
                            ollama_reply = f"✏️ Updated: {subject} ({updated.get('id')})"
                            st.success(ollama_reply)

                elif intent == "delete":
                    if not subject:
                        ollama_reply = "⚠️ Please specify the event subject to delete."
                        st.error(ollama_reply)
                    else:
                        events = search_event(USER_ID, token, subject)
                        if not events:
                            ollama_reply = f"⚠️ No event found with subject '{subject}'"
                            st.warning(ollama_reply)
                        else:
                            event_id = events[0]['id']
                            delete_event(USER_ID, token, event_id)
                            ollama_reply = f"🗑 Deleted: {subject}"
                            st.success(ollama_reply)
                else:
                    ollama_reply = "🤔 I couldn’t understand your intent clearly."
                    st.info(ollama_reply)

            except Exception as e:
                ollama_reply = f"❌ Action failed: {e}"
                st.error(ollama_reply)

            # ✅ Add to chat history for all outcomes
            st.session_state['chat_history'].append({"user": text, "ollama": ollama_reply})

# Display the entire chat history (latest first)
st.write("## Chat History")
for chat in reversed(st.session_state['chat_history']):
    st.write(f"**🧑 User:** {chat['user']}")
    st.write(f"**🧠 Assistant:** {chat['ollama']}")
    st.write("---")