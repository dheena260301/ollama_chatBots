import os
import datetime
import json
import requests
import streamlit as st
from dotenv import load_dotenv
from msal import ConfidentialClientApplication
from dateutil import parser as dateparser
import re

# Load environment
load_dotenv()

AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
AZURE_USER_ID = os.getenv("AZURE_USER_ID")
OUTLOOK_TIMEZONE = os.getenv("OUTLOOK_TIMEZONE", "Asia/Kolkata")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", 0.2))

# Microsoft Graph token
def get_app_token():
    app = ConfidentialClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        client_credential=AZURE_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    return result.get("access_token")

# Ollama intent detection
def analyze_user_intent_with_ollama(prompt: str):
    """
    Uses Ollama to determine user's intent (create/edit/delete/list/summarize)
    and returns structured JSON. Handles malformed JSON responses gracefully.
    """
    try:
        system_prompt = """
You are an Outlook Calendar AI Assistant.
Determine if the user wants to create, edit, delete, list, or summarize meetings.

Use these synonyms:
- Create: add, schedule, plan, set up
- Edit: modify, update, change, reschedule
- Delete: cancel, remove, discard
- List/Summarize: show, display, view, list, summarize, overview, report

Return JSON only, in this format:
{"intent": "<create|edit|delete|list|summarize>", "details": {}}

### Examples:
User: "schedule a meeting tomorrow at 3pm" → {"intent": "create"}
User: "update my 10am meeting to 11am" → {"intent": "edit"}
User: "cancel meeting with John" → {"intent": "delete"}
User: "show my calendar" → {"intent": "list"}
User: "summarize my meetings" → {"intent": "summarize"}
        """

        payload = {
            "model": OLLAMA_MODEL,
            "prompt": system_prompt + f"\nUser: {prompt}\nAssistant:",
            "stream": False,
            "temperature": OLLAMA_TEMPERATURE,
        }

        # --- Call Ollama API ---
        res = requests.post("http://localhost:11434/api/generate", json=payload, timeout=60)
        data = res.json()
        text = data.get("response", "").strip()

        # --- Extract JSON block safely ---
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            json_text = text[start:end + 1]

            # Fix malformed JSON (single quotes, missing quotes around keys)
            json_text = re.sub(r"'", '"', json_text)
            json_text = re.sub(r"(\w+):", r'"\1":', json_text)
            json_text = re.sub(r",\s*([}\]])", r"\1", json_text)

            try:
                parsed = json.loads(json_text)
            except Exception:
                st.warning(f"⚠️ Could not parse Ollama intent JSON:\n{text}")
                parsed = {"intent": "unknown"}
        else:
            st.warning(f"⚠️ No valid JSON found in Ollama response:\n{text}")
            parsed = {"intent": "unknown"}

        # --- Normalize summarize to list ---
        if parsed.get("intent") == "summarize":
            parsed["intent"] = "list"

        return parsed

    except Exception as e:
        return {"error": f"Ollama intent parsing failed: {e}", "raw": ""}

# Ask Ollama for structured event details for create/edit/delete
def ask_ollama_for_event_details(prompt: str):
    """
    Robust extractor for meeting details.
    Handles invalid JSON (single quotes, trailing commas),
    converts natural phrases ('tomorrow 7pm') → ISO datetimes,
    and pulls email addresses when attendees missing.
    """
    try:
        # Detect local timezone
        local_tz = datetime.datetime.now().astimezone().tzinfo
        tz_name = getattr(local_tz, "key", str(local_tz)) or "UTC"
        now_local = datetime.datetime.now(local_tz).strftime("%Y-%m-%d %H:%M:%S %z")

        # Build Ollama system prompt
        system = f"""
You are a scheduling assistant. Current local datetime: {now_local} ({tz_name}).
Extract meeting information as valid JSON only.
Fields: subject, start_datetime (ISO8601), end_datetime (ISO8601), duration_minutes, attendees, body, location.
Return strictly JSON (no text, no markdown, no commentary).
        """

        payload = {
            "model": OLLAMA_MODEL,
            "prompt": system + f"\nUser: {prompt}\nAssistant:",
            "stream": False,
            "temperature": OLLAMA_TEMPERATURE,
        }

        res = requests.post("http://localhost:11434/api/generate", json=payload, timeout=60)
        text = res.json().get("response", "").strip()

        # --- Extract JSON safely ---
        start_idx, end_idx = text.find("{"), text.rfind("}")
        if start_idx == -1 or end_idx == -1:
            st.warning("⚠️ Ollama did not return a valid JSON block.")
            return {}

        json_text = text[start_idx:end_idx+1]

        # --- Sanitize malformed JSON ---
        json_text = re.sub(r"(\w+):", r'"\1":', json_text)     # wrap unquoted keys
        json_text = re.sub(r"'", '"', json_text)                # single → double quotes
        json_text = re.sub(r",\s*([}\]])", r"\1", json_text)    # remove trailing commas

        try:
            details = json.loads(json_text)
        except json.JSONDecodeError:
            st.warning(f"⚠️ Could not parse Ollama JSON:\n{text}")
            return {}

        # --- Normalize date/time values ---
        def normalize(value):
            if not value:
                return None
            try:
                dt = dateparser.parse(value, default=datetime.datetime.now(local_tz))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=local_tz)
                else:
                    dt = dt.astimezone(local_tz)
                return dt
            except Exception:
                return None

        start_dt = normalize(details.get("start_datetime"))
        end_dt = normalize(details.get("end_datetime"))

        if start_dt and not end_dt:
            end_dt = start_dt + datetime.timedelta(minutes=int(details.get("duration_minutes", 30)))

        if start_dt:
            details["start_datetime"] = start_dt.isoformat(timespec="seconds")
        if end_dt:
            details["end_datetime"] = end_dt.isoformat(timespec="seconds")

        # --- Add attendees from user text if missing ---
        if not details.get("attendees"):
            emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", prompt)
            if emails:
                details["attendees"] = list(dict.fromkeys(emails))  # unique list

        return details

    except Exception as e:
        st.warning(f"⚠️ Failed to extract details: {e}")
        return {}

# Fetch events from Graph
def fetch_user_events(user_id, token, start_dt, end_dt):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView"
    params = {"startDateTime": start_dt.isoformat(), "endDateTime": end_dt.isoformat(), "$orderby": "start/dateTime"}
    headers = {"Authorization": f"Bearer {token}", "Prefer": f'outlook.timezone="{OUTLOOK_TIMEZONE}"'}
    res = requests.get(url, headers=headers, params=params)
    if res.status_code != 200:
        raise Exception(res.text)
    return res.json().get("value", [])

# Graph CRUD
def create_event_graph(user_id, token, event_payload: dict):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    res = requests.post(url, headers=headers, json=event_payload)
    if res.status_code not in (200,201):
        raise Exception(res.text)
    return res.json()

def update_event_graph(user_id, token, event_id: str, changes: dict):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    res = requests.patch(url, headers=headers, json=changes)
    if res.status_code != 200:
        raise Exception(res.text)
    return res.json()

def delete_event_graph(user_id, token, event_id: str):
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/events/{event_id}"
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.delete(url, headers=headers)
    if res.status_code not in (204,200):
        raise Exception(res.text)
    return True

def search_events_by_subject(events, subject):
    s = subject.lower()
    return [e for e in events if s in e.get("subject","").lower()]

# Shape and categorize
def shape_events(events):
    now = datetime.datetime.now()
    categorized = {"upcoming": {}, "completed": {}, "cancelled": {}}
    for e in events:
        subject = e.get("subject","No Title")
        body_preview = e.get("bodyPreview","")
        start_str = e.get("start",{}).get("dateTime")
        end_str = e.get("end",{}).get("dateTime")
        status = e.get("showAs","").lower()
        cancelled_flag = e.get("isCancelled", False)
        if not start_str or not end_str:
            continue
        start = dateparser.parse(start_str)
        end = dateparser.parse(end_str)
        date_key = start.strftime("%B %d, %Y")
        line = f"• {subject} ({start.strftime('%I:%M %p')} - {end.strftime('%I:%M %p')})"
        if body_preview:
            line += f" — {body_preview[:60].strip()}"
        if cancelled_flag or "cancelled" in status:
            categorized["cancelled"].setdefault(date_key, []).append(line)
        elif end < now:
            categorized["completed"].setdefault(date_key, []).append(line)
        else:
            categorized["upcoming"].setdefault(date_key, []).append(line)
    return categorized

# Build Graph payload from details
def build_graph_event_payload(details: dict):
    """
    Build a Microsoft Graph event payload from parsed details.
    """
    local_tz = datetime.datetime.now().astimezone().tzinfo
    tz_name = getattr(local_tz, "key", str(local_tz)) or "UTC"
    payload = {}

    def safe_parse_dt(s):
        try:
            dt = dateparser.parse(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_tz)
            else:
                dt = dt.astimezone(local_tz)
            return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            st.warning(f"⚠️ Invalid date format received: {s}")
            return None

    # --- Start / End ---
    if details.get("start_datetime"):
        dt = safe_parse_dt(details["start_datetime"])
        if dt:
            payload["start"] = {"dateTime": dt, "timeZone": tz_name}

    if details.get("end_datetime"):
        dt = safe_parse_dt(details["end_datetime"])
        if dt:
            payload["end"] = {"dateTime": dt, "timeZone": tz_name}
    elif details.get("start_datetime"):
        try:
            sd = dateparser.parse(details["start_datetime"])
            if sd.tzinfo is None:
                sd = sd.replace(tzinfo=local_tz)
            ed = sd + datetime.timedelta(minutes=int(details.get("duration_minutes", 30)))
            payload["end"] = {
                "dateTime": ed.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": tz_name,
            }
        except Exception:
            pass

    # --- Subject ---
    if details.get("subject"):
        payload["subject"] = details["subject"]

    # --- Body ---
    if details.get("body"):
        payload["body"] = {"contentType": "text", "content": details["body"]}

    # --- Location ---
    if details.get("location"):
        payload["location"] = {"displayName": details["location"]}

    # --- Attendees ---
    if details.get("attendees"):
        attendees = []
        for a in details["attendees"]:
            if not a:
                continue
            attendees.append({
                "emailAddress": {"address": a, "name": a},
                "type": "required"
            })
        if attendees:
            payload["attendees"] = attendees

    return payload

# Display summary with expanders
def display_summary(events, period_desc, start_dt=None, end_dt=None):
    categorized = shape_events(events)
    st.markdown(f"### 🧠 Summary of meetings ({period_desc})")
    if start_dt and end_dt:
        st.markdown(f"📅 From **{start_dt.strftime('%b %d, %Y')}** to **{end_dt.strftime('%b %d, %Y')}**")
    sections = [("🟢 Upcoming Meetings","upcoming"),("🔵 Completed Meetings","completed"),("🔴 Cancelled Meetings","cancelled")]
    has_meetings = False
    for title,key in sections:
        with st.expander(title, expanded=(key=="upcoming")):
            data = categorized[key]
            if not data:
                st.info("None")
            else:
                has_meetings = True
                for date, items in sorted(data.items()):
                    st.markdown(f"**{date}:**")
                    for line in items:
                        st.write(line)
                    st.markdown("---")
    if not has_meetings:
        st.success("✅ No meetings found in this period.")

def ask_ollama_for_event_details(prompt: str):
    """
    Robustly extracts meeting details from Ollama and normalizes datetimes.
    Handles invalid JSON, relative times ('tomorrow 7pm'), and missing fields.
    """
    try:
        # Detect system local timezone
        local_tz = datetime.datetime.now().astimezone().tzinfo
        tz_name = getattr(local_tz, "key", str(local_tz)) or "UTC"
        now_local = datetime.datetime.now(local_tz).strftime("%Y-%m-%d %H:%M:%S %z")

        # Tell Ollama to produce strict JSON
        system = f"""
You are a scheduling assistant. Current local datetime: {now_local} ({tz_name})
Extract these fields if available:
- subject
- start_datetime (ISO 8601, e.g. 2025-11-09T19:00:00)
- end_datetime (ISO 8601)
- duration_minutes (int)
- attendees (array of emails)
- body
- location
If any are missing, omit them.
Return ONLY valid JSON (no markdown, no commentary).
        """

        payload = {
            "model": OLLAMA_MODEL,
            "prompt": system + f"\nUser: {prompt}\nAssistant:",
            "stream": False,
            "temperature": OLLAMA_TEMPERATURE,
        }

        res = requests.post("http://localhost:11434/api/generate", json=payload, timeout=60)
        text = res.json().get("response", "").strip()

        # --- Extract only JSON block safely ---
        start_idx, end_idx = text.find("{"), text.rfind("}")
        if start_idx == -1 or end_idx == -1:
            st.warning("⚠️ Ollama returned no valid JSON block.")
            details = {}
        else:
            raw_json = text[start_idx:end_idx+1]
            # Replace single quotes → double quotes, remove trailing commas
            raw_json = re.sub(r"'", '"', raw_json)
            raw_json = re.sub(r",\s*}", "}", raw_json)
            raw_json = re.sub(r",\s*]", "]", raw_json)
            try:
                details = json.loads(raw_json)
            except Exception:
                details = {}

        # --- If no start time, fallback to parsing user prompt ---
        if not details.get("start_datetime"):
            m = re.search(r"(tomorrow|today|in\s+\d+\s+days?)", prompt.lower())
            time_m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", prompt.lower())
            base = datetime.datetime.now(local_tz)
            if m:
                if "tomorrow" in m.group(1):
                    base += datetime.timedelta(days=1)
                elif "in" in m.group(1):
                    n = int(re.search(r"in\s+(\d+)", m.group(1)).group(1))
                    base += datetime.timedelta(days=n)
            hour = 9
            minute = 0
            if time_m:
                hour = int(time_m.group(1))
                minute = int(time_m.group(2) or 0)
                ampm = time_m.group(3)
                if ampm == "pm" and hour < 12:
                    hour += 12
                if ampm == "am" and hour == 12:
                    hour = 0
            start_dt = datetime.datetime.combine(base.date(), datetime.time(hour, minute, tzinfo=local_tz))
            details["start_datetime"] = start_dt.isoformat(timespec="seconds")

        # --- Normalize all date fields to ISO 8601 ---
        def normalize_dt(val):
            if not val:
                return None
            try:
                dt = dateparser.parse(val, default=datetime.datetime.now(local_tz))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=local_tz)
                return dt.isoformat(timespec="seconds")
            except Exception:
                return None

        details["start_datetime"] = normalize_dt(details.get("start_datetime"))
        details["end_datetime"] = normalize_dt(details.get("end_datetime"))

        # Add default 30 minutes if no end time
        if details.get("start_datetime") and not details.get("end_datetime"):
            sd = dateparser.parse(details["start_datetime"])
            ed = sd + datetime.timedelta(minutes=int(details.get("duration_minutes", 30)))
            details["end_datetime"] = ed.isoformat(timespec="seconds")

        # Extract emails from the prompt if attendees missing
        if not details.get("attendees"):
            emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", prompt)
            if emails:
                details["attendees"] = list(dict.fromkeys(emails))  # remove duplicates, preserve order

        return details

    except Exception as e:
        st.warning(f"⚠️ Failed to extract details: {e}")
        return {}

# Date range extraction via Ollama (few-shot)
def extract_date_range_with_ollama(prompt: str):
    try:
        today = datetime.date.today().isoformat()
        system = f"""
You are a date range extraction assistant. Today's date: {today}.
Convert user's natural language into JSON: {"start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","description":"text"}
If unspecified, default to last 30 days.
Examples:
User: "last week" -> start_date = (today - 7 days)
User: "from Oct 1 to Nov 5" -> exact dates
"""
        payload = {"model": OLLAMA_MODEL, "prompt": system + f"\nUser: {prompt}\nAssistant:", "stream": False, "temperature": OLLAMA_TEMPERATURE}
        res = requests.post("http://localhost:11434/api/generate", json=payload, timeout=60)
        text = res.json().get("response", "")
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        parsed = json.loads(text[start:end+1])
        sd = dateparser.parse(parsed["start_date"]) if "start_date" in parsed else datetime.datetime.now() - datetime.timedelta(days=30)
        ed = dateparser.parse(parsed["end_date"]) if "end_date" in parsed else datetime.datetime.now()
        return {"start_date": sd, "end_date": ed, "description": parsed.get("description", "custom range")}
    except Exception:
        return None

# Streamlit UI
st.set_page_config(page_title="Calendar Assistant", page_icon="📅", layout="wide")
st.title("📅 Calendar Assistance (Ollama + Graph)")

with st.form("llm_form"):
    text = st.text_area("Enter Your Prompt (e.g., 'Create meeting tomorrow at 7pm with Jana')")
    submit = st.form_submit_button("Submit")

if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []

if submit and text:
    with st.spinner("🧠 Ollama analyzing your request..."):
        token = get_app_token()
        analysis = analyze_user_intent_with_ollama(text)
        ollama_reply = ""
        if "error" in analysis:
            ollama_reply = f"❌ {analysis['error']}"
            st.error(ollama_reply)
        else:
            intent = analysis.get("intent", "").lower()
            try:
                if intent in ["list","summarize","show","overview","view"]:
                    dr = extract_date_range_with_ollama(text)
                    if dr:
                        start_dt = dr["start_date"]
                        end_dt = dr["end_date"]
                        desc = dr.get("description","custom range")
                    else:
                        start_dt = datetime.datetime.now() - datetime.timedelta(days=30)
                        end_dt = datetime.datetime.now()
                        desc = "last 30 days"
                    events = fetch_user_events(AZURE_USER_ID, token, start_dt, end_dt)
                    display_summary(events, desc, start_dt=start_dt, end_dt=end_dt)
                    ollama_reply = f"✅ Displayed categorized meeting summary ({desc})."

                elif intent == "create":
                    details = ask_ollama_for_event_details(text)
                    if not details or "start_datetime" not in details:
                        ollama_reply = "⚠️ Could not extract full event details (start time missing)."
                        st.warning(ollama_reply)
                    else:
                        payload = build_graph_event_payload(details)
                        created = create_event_graph(AZURE_USER_ID, token, payload)

                        subject = created.get("subject", "Untitled Event")
                        start = created.get("start", {}).get("dateTime", "")
                        end = created.get("end", {}).get("dateTime", "")
                        location = created.get("location", {}).get("displayName", "")
                        attendees = [
                            a["emailAddress"]["address"]
                            for a in created.get("attendees", [])
                            if "emailAddress" in a
                        ]
                        body_preview = created.get("body", {}).get("content", "")

                        # Convert times for readability
                        try:
                            start_dt = dateparser.parse(start).strftime("%B %d, %Y (%I:%M %p)")
                            end_dt = dateparser.parse(end).strftime("%I:%M %p")
                        except Exception:
                            start_dt, end_dt = start, end

                        # 🧠 Build a friendly summary
                        st.success(f"✅ **Meeting Created Successfully!**")
                        st.markdown(f"""
                        ### 📅 {subject}
                        **🕒 When:** {start_dt} - {end_dt}  
                        **📍 Location:** {location or 'Not specified'}  
                        **👥 Attendees:** {', '.join(attendees) if attendees else 'No attendees specified'}  
                        **📝 Notes:** {body_preview[:120] + '...' if body_preview else 'No description provided.'}
                        """)
                        ollama_reply = f"✅ Created and summarized meeting: {subject}"

                elif intent == "edit":
                    details = ask_ollama_for_event_details(text)
                    subj = details.get("subject")

                    if not subj:
                        ollama_reply = "⚠️ Please include the event subject to update."
                        st.warning(ollama_reply)
                    else:
                        evs = fetch_user_events(
                            AZURE_USER_ID,
                            token,
                            datetime.datetime.now() - datetime.timedelta(days=365),
                            datetime.datetime.now() + datetime.timedelta(days=365),
                        )
                        matches = search_events_by_subject(evs, subj)

                        if not matches:
                            ollama_reply = f"⚠️ No events found with subject containing '{subj}'"
                            st.warning(ollama_reply)
                        else:
                            event_id = matches[0]["id"]
                            payload = build_graph_event_payload(details)
                            updated = update_event_graph(AZURE_USER_ID, token, event_id, payload)

                            subject = updated.get("subject", subj)
                            start = updated.get("start", {}).get("dateTime", "")
                            end = updated.get("end", {}).get("dateTime", "")
                            location = updated.get("location", {}).get("displayName", "")
                            attendees = [
                                a["emailAddress"]["address"]
                                for a in updated.get("attendees", [])
                                if "emailAddress" in a
                            ]
                            body_preview = updated.get("body", {}).get("content", "")

                            # Format times
                            try:
                                start_dt = dateparser.parse(start).strftime("%B %d, %Y (%I:%M %p)")
                                end_dt = dateparser.parse(end).strftime("%I:%M %p")
                            except Exception:
                                start_dt, end_dt = start, end

                            # 🎯 Show update success + summary
                            st.success("✏️ **Meeting Updated Successfully!**")
                            st.markdown(f"""
                            ### 📅 {subject}
                            **🕒 When:** {start_dt} - {end_dt}  
                            **📍 Location:** {location or 'Not specified'}  
                            **👥 Attendees:** {', '.join(attendees) if attendees else 'No attendees specified'}  
                            **📝 Notes:** {body_preview[:120] + '...' if body_preview else 'No description provided.'}
                            """)

                            ollama_reply = f"✏️ Updated and summarized meeting: {subject}"


                elif intent == "delete":
                    details = ask_ollama_for_event_details(text)
                    subj = details.get("subject")
                    if not subj:
                        ollama_reply = "⚠️ Please specify the subject of the event to delete."
                        st.warning(ollama_reply)
                    else:
                        evs = fetch_user_events(AZURE_USER_ID, token, datetime.datetime.now() - datetime.timedelta(days=365), datetime.datetime.now() + datetime.timedelta(days=365))
                        matches = search_events_by_subject(evs, subj)
                        if not matches:
                            ollama_reply = f"⚠️ No events found with subject containing '{subj}'"
                            st.warning(ollama_reply)
                        else:
                            event_id = matches[0]["id"]
                            delete_event_graph(AZURE_USER_ID, token, event_id)
                            ollama_reply = f"🗑️ Deleted event matching '{subj}'"
                            st.success(ollama_reply)

                else:
                    ollama_reply = "🤔 I couldn’t determine your intent clearly."
                    st.warning(ollama_reply)
            except Exception as e:
                ollama_reply = f"❌ Action failed: {e}"
                st.error(ollama_reply)
        st.session_state["chat_history"].append({"user": text, "ollama": ollama_reply})

# Chat history
st.write("## 💬 Chat History")
for chat in reversed(st.session_state["chat_history"]):
    st.write(f"**🧑 User:** {chat['user']}")
    st.write(f"**🧠 Assistant:** {chat['ollama']}")
    st.write("---")
