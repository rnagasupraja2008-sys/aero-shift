import datetime
import json

import pandas as pd
import requests
import streamlit as st

APP_NAME = "AeroShift"
BUCKET = "your-bucket-name-here"  # change after you create your S3 bucket

st.set_page_config(page_title=APP_NAME, page_icon="🌫️")
st.title(f"🌫️ {APP_NAME}")
st.caption("Live air quality → likely cause → what YOU should do")


# ---------- BACKEND 1: get data from the internet ----------
def get_location(name):
    """City name -> (latitude, longitude, proper name)"""
    r = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": name, "count": 1},
        timeout=10,
    ).json()
    if not r.get("results"):
        return None
    p = r["results"][0]
    return p["latitude"], p["longitude"], p["name"]


def get_air(lat, lon):
    """Latitude/longitude -> pollution readings (now + hourly AQI)"""
    return requests.get(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "us_aqi,pm2_5,pm10,nitrogen_dioxide,sulphur_dioxide,carbon_monoxide,dust",
            "hourly": "us_aqi",
            "forecast_days": 1,
            "timezone": "auto",
        },
        timeout=10,
    ).json()


# ---------- BACKEND 2: the "brain" (simple rules) ----------
def aqi_label(aqi):
    if aqi <= 50:
        return "🟢 Good"
    if aqi <= 100:
        return "🟡 Moderate"
    if aqi <= 150:
        return "🟠 Unhealthy for sensitive groups"
    if aqi <= 200:
        return "🔴 Unhealthy"
    if aqi <= 300:
        return "🟣 Very unhealthy"
    return "🟤 Hazardous"


def find_cause(c):
    """Rough guess of the main pollution source. Thresholds are
    approximate, so call this a 'likely cause', not a proven one."""
    pm25, pm10 = c.get("pm2_5") or 0, c.get("pm10") or 0
    no2, so2 = c.get("nitrogen_dioxide") or 0, c.get("sulphur_dioxide") or 0
    co, dust = c.get("carbon_monoxide") or 0, c.get("dust") or 0
    ratio = pm25 / pm10 if pm10 else 0  # small = coarse dust, big = fine smoke

    causes = []
    if dust > 50 or (pm10 > 100 and ratio < 0.4):
        causes.append("🏗️ Dust (construction / dry roads)")
    if no2 > 40:
        causes.append("🚗 Vehicle exhaust")
    if so2 > 20:
        causes.append("🏭 Industrial emissions")
    if ratio > 0.6 and co > 1000:
        causes.append("🔥 Burning (crop / garbage)")
    return causes or ["❓ Mixed or unclear sources"]


# "limit" = the highest AQI at which this person can reasonably go outside.
# Sensitive groups get a lower limit. Change these numbers if you want.
MIDDLE_AGED = {
    "limit": 100,
    "caution": "Keep outdoor exercise light and take breaks. Stop and rest if you feel dizzy, "
    "breathless, or your chest feels tight. Wear an N95 on busy roads.",
    "severe": "Stay indoors where you can and skip outdoor exercise. If you have high BP, diabetes "
    "or heart trouble, follow your doctor's plan and keep your medicines on hand.",
}

PERSONAS = {
    "Healthy adult": {
        "limit": 150,
        "caution": "Short outdoor trips are fine, but cut back on long or hard outdoor exercise. "
        "Wear an N95 on busy roads.",
        "severe": "Skip outdoor workouts and exercise indoors instead. Limit trips outside, and "
        "wear a fitted N95 if you must go.",
    },
    "Middle-aged man": MIDDLE_AGED,
    "Middle-aged woman": MIDDLE_AGED,
    "Senior (60+)": {
        "limit": 100,
        "caution": "Stay indoors during the worst hours. Take only short walks in the cleanest "
        "hour, and keep your regular medicines within reach.",
        "severe": "Stay indoors with windows shut and ask family or neighbours to do errands. Call "
        "a doctor if you feel breathless, dizzy or confused, and get urgent help (112) for chest pain.",
    },
    "Child (under 12)": {
        "limit": 100,
        "caution": "Move sports and playground time indoors, or to the cleanest hour. Use a "
        "kid-sized mask that fits well (adult masks often don't) when travelling.",
        "severe": "Keep them indoors with windows shut and no outdoor play. See a doctor if they "
        "cough a lot, wheeze or breathe fast.",
    },
    "Asthma patient": {
        "limit": 100,
        "caution": "Carry your reliever inhaler and take your regular medicines. Avoid going out in "
        "bad hours and stay away from dusty or smoky places.",
        "severe": "Stay indoors, keep your inhaler within reach, and follow your doctor's asthma "
        "action plan. Get urgent help if your inhaler isn't helping.",
    },
    "Heart patient": {
        "limit": 100,
        "caution": "Avoid hard exertion and long outdoor walks. Go out only in the cleanest hour, "
        "and take your medicines as prescribed.",
        "severe": "Stay indoors and rest. Get urgent help (112) for chest pain, severe breathlessness "
        "or fainting.",
    },
    "Other patient (lung, diabetes, etc.)": {
        "limit": 100,
        "caution": "Treat yourself as a sensitive person: limit time outdoors, wear an N95, and "
        "follow your doctor's advice for your condition.",
        "severe": "Stay indoors and keep your medicines and your doctor's number handy. Ask your "
        "doctor what to do on very polluted days.",
    },
    "Two-wheeler commuter": {
        "limit": 150,
        "caution": "Wear a properly fitted N95 (cloth masks don't help much). Pick quieter side "
        "roads over busy ones.",
        "severe": "Avoid riding in peak traffic hours or use public transport if you can. If you "
        "must ride, wear an N95, go slow on quieter roads, and wash your face and hands on arrival.",
    },
}

EXTRAS = {
    "caution": [
        "Keep windows closed in the worst hours and air the house in the cleanest hour",
        "Drink plenty of water",
        "Avoid exercising near busy roads and traffic junctions",
        "Don't burn garbage, leaves or wood",
    ],
    "severe": [
        "Run an air purifier if you have one, and keep windows and doors shut",
        "Wet-mop or damp-wipe instead of dry sweeping, which kicks up dust",
        "Avoid outdoor exercise and crowded roadsides",
        "Cut indoor smoke: no burning, no smoking, no incense",
        "Wash your hands and face after coming indoors",
    ],
}


def band(aqi):
    if aqi <= 100:
        return "ok"
    return "caution" if aqi <= 200 else "severe"


def hour_ranges(hours):
    """[8, 9, 10, 15] -> '08:00-11:00, 15:00-16:00'"""
    ranges, start, prev = [], None, None
    for h in hours:
        if start is None:
            start = prev = h
        elif h == prev + 1:
            prev = h
        else:
            ranges.append((start, prev))
            start = prev = h
    if start is not None:
        ranges.append((start, prev))
    return ", ".join(f"{a:02d}:00-{(b + 1) % 24:02d}:00" for a, b in ranges)


# ---------- BACKEND 3: save community reports ----------
def save_report(report):
    try:
        import boto3

        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        boto3.client("s3").put_object(
            Bucket=BUCKET, Key=f"reports/{stamp}.json", Body=json.dumps(report)
        )
        return "✅ Report saved to Amazon S3"
    except Exception as e:
        with open("reports_local.json", "a") as f:
            f.write(json.dumps(report) + "\n")
        return f"Saved locally (S3 not set up yet: {type(e).__name__})"


# ---------- FRONTEND: what the user sees ----------
city = st.text_input("Your city", "Delhi")
persona = st.selectbox("Who is this advice for?", list(PERSONAS.keys()))

if st.button("Check my air"):
    try:
        place = get_location(city)
        if not place:
            st.error("City not found. Try another spelling.")
        else:
            lat, lon, name = place
            # session_state remembers results, so they stay on screen
            # even after you submit the report form below
            st.session_state["result"] = {"name": name, "data": get_air(lat, lon)}
    except requests.RequestException:
        st.error("Couldn't reach the air-quality service. Check your internet and try again.")

result = st.session_state.get("result")
if result:
    data = result["data"]
    if "current" not in data:
        st.error("No air data came back for this place. Try another city.")
    else:
        now = data["current"]
        aqi = now["us_aqi"]
        info = PERSONAS[persona]

        st.subheader(f"{result['name']}: AQI {aqi}")
        st.write(aqi_label(aqi))
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PM2.5", now["pm2_5"])
        c2.metric("PM10", now["pm10"])
        c3.metric("NO₂", now["nitrogen_dioxide"])
        c4.metric("SO₂", now["sulphur_dioxide"])

        st.markdown("### Likely cause")
        for cause in find_cause(now):
            st.write(cause)

        st.markdown(f"### What you should do ({persona})")
        b = band(aqi)
        if b == "ok":
            st.success("✅ Air is acceptable. Normal activity is fine.")
        elif b == "caution":
            st.warning("⚠️ " + info["caution"])
        else:
            st.error("🚨 " + info["severe"])
        if b != "ok":
            with st.expander("More precautions for everyone at home"):
                for tip in EXTRAS[b]:
                    st.write("• " + tip)

        st.markdown("### Best time to go outside (for you)")
        df = pd.DataFrame(
            {"time": pd.to_datetime(data["hourly"]["time"]), "AQI": data["hourly"]["us_aqi"]}
        ).dropna()
        df["hour"] = df["time"].dt.strftime("%H:%M")
        upcoming = df[df["time"] >= pd.to_datetime(now["time"]).floor("h")]
        if upcoming.empty:
            upcoming = df
        ok = upcoming[upcoming["AQI"] <= info["limit"]]
        if not ok.empty:
            best = ok.loc[ok["AQI"].idxmin()]
            st.success(f"Best hour: {best['hour']} (AQI {int(best['AQI'])})")
            st.write("Okay hours left today: " + hour_ranges(ok["time"].dt.hour.tolist()))
        else:
            best = upcoming.loc[upcoming["AQI"].idxmin()]
            st.warning(
                f"No safe hours left today for you. Least bad time: {best['hour']} "
                f"(AQI {int(best['AQI'])})"
            )
        st.caption(
            "Air is often worse in early morning and late evening, but check the chart "
            "instead of guessing. This is general guidance, not medical advice. "
            "Ask your doctor if you have a health condition."
        )
        st.line_chart(df.set_index("hour")["AQI"])

st.divider()
st.markdown("### 📢 Report a problem near you")
with st.form("report"):
    kind = st.selectbox("What did you see?", ["Garbage burning", "Stubble burning", "Construction dust"])
    where = st.text_input("Where? (area / landmark)")
    note = st.text_area("Anything else?")
    if st.form_submit_button("Submit report"):
        st.write(
            save_report(
                {
                    "type": kind,
                    "where": where,
                    "note": note,
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
            )
        )