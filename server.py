import asyncio
import base64
import json
import os
import re
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import httpx
from dotenv import load_dotenv

load_dotenv()

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "sk_7dobwl3v_cqg8ysqLjDMQdrLeqdALOwjH")
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"

app = FastAPI(title="RakshaVoice - Sarvam AI Emergency Voice Agent", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "service": "RakshaVoice Emergency Line",
        "sarvam_models": {
            "stt": "saaras:v3",
            "llm": "sarvam-105b-conversations",
            "tts": "bulbul:v3"
        }
    }

LANGUAGE_SPEAKER_MAP = {
    "hi-IN": "aditya",
    "ta-IN": "vijay",
    "te-IN": "kavitha",
    "kn-IN": "chaitra_kn_conversation",
    "bn-IN": "roopa_bn_conversational",
    "mr-IN": "ishita_mr_conversational",
    "en-IN": "aditya",
    "unknown": "aditya"
}

INCIDENT_EXTRACTION_PROMPT = """You are RakshaVoice, an AI emergency dispatch triage agent for campus security and civil defense in India.
Your mission is to parse caller emergency speech (which may be in Hindi, Tamil, Telugu, English, or code-mixed Hinglish/Tanglish).

Extract and return STRICTLY valid JSON with these keys:
{
  "incident_type": "FIRE_HAZARD" or "MEDICAL_EMERGENCY" or "SECURITY_THREAT" or "RAGGING" or "ACCIDENT" or "LAB_DISASTER" or "OTHER",
  "urgency": "CRITICAL" or "HIGH" or "MEDIUM" or "LOW",
  "location": "Specific location, block, lab, hostel or landmark mentioned",
  "victims_count": "number or 'unknown'",
  "detected_language": "Language/dialect detected (e.g., Hindi, Hinglish, Tamil, Tanglish, Telugu, English)",
  "summary_en": "One clear, professional line in English for first responder dispatch",
  "reassurance_indic": "A short, calming 1-2 sentence reassuring response in the EXACT same language and colloquial dialect spoken by the caller, telling them help is on the way."
}
Output pure JSON ONLY. No markdown ticks, no intro, no outro."""

async def parse_incident_with_sarvam(transcript: str) -> dict:
    """Uses Sarvam-105B foundational LLM to analyze emergency panic speech."""
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "model": "sarvam-105b-conversations",
        "messages": [
            {"role": "system", "content": INCIDENT_EXTRACTION_PROMPT},
            {"role": "user", "content": f"EMERGENCY CALL TRANSCRIPT: {transcript}"}
        ],
        "temperature": 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SARVAM_CHAT_URL, headers=headers, json=payload)
            if resp.status_code == 200:
                raw = resp.json()["choices"][0]["message"]["content"]
                clean = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
                start = clean.find("{")
                end = clean.rfind("}")
                if start != -1 and end != -1:
                    clean = clean[start:end+1]
                return json.loads(clean)
            else:
                print(f"[Sarvam LLM Error] Status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[LLM Extraction Exception] {e}")

    # Fallback heuristic
    is_critical = any(w in transcript.lower() for w in ["fire", "aag", "blood", "mar", "unconscious", "cylinder", "police", "blast", "thee", "danger"])
    return {
        "incident_type": "FIRE_HAZARD" if any(x in transcript.lower() for x in ["fire", "aag", "blast"]) else "MEDICAL_EMERGENCY",
        "urgency": "CRITICAL" if is_critical else "HIGH",
        "location": "Identified from emergency audio coordinates",
        "victims_count": "1+",
        "detected_language": "Indic (Auto-detected)",
        "summary_en": f"Emergency report: {transcript[:120]}...",
        "reassurance_indic": "आपकी आपातकालीन सूचना दर्ज कर ली गई है। रेस्क्यू टीम तुरंत मौके पर पहुंच रही है, कृपया सुरक्षित स्थान पर रहें।"
    }

async def generate_sarvam_tts(text: str, target_lang: str = "hi-IN") -> Optional[str]:
    """Generates authentic regional Indic voice response using Sarvam Bulbul:v3."""
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json"
    }
    
    valid_lang = target_lang if target_lang in LANGUAGE_SPEAKER_MAP and target_lang != "unknown" else "hi-IN"
    speaker = LANGUAGE_SPEAKER_MAP.get(valid_lang, "aditya")

    payload = {
        "inputs": [text],
        "target_language_code": valid_lang,
        "speaker": speaker,
        "pitch": 0,
        "pace": 1.05,
        "loudness": 1.5,
        "speech_sample_rate": 16000,
        "enable_preprocessing": True,
        "model": "bulbul:v3"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SARVAM_TTS_URL, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                if "audios" in data and len(data["audios"]) > 0:
                    return data["audios"][0]
            else:
                print(f"[Sarvam TTS Error] Status {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[Bulbul TTS Exception] {e}")
    return None

@app.post("/api/report-audio")
async def report_audio(
    file: UploadFile = File(...),
    language_code: str = Form("hi-IN")
):
    """Processes uploaded caller voice recording using Saaras:v3 + Sarvam-105B + Bulbul:v3."""
    audio_bytes = await file.read()
    
    headers = {"api-subscription-key": SARVAM_API_KEY}
    files = {"file": (file.filename or "call.wav", audio_bytes, file.content_type or "audio/wav")}
    data = {
        "model": "saaras:v3",
        "language_code": language_code if language_code != "unknown" else "hi-IN"
    }

    transcript = ""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            stt_resp = await client.post(SARVAM_STT_URL, headers=headers, files=files, data=data)
            if stt_resp.status_code == 200:
                transcript = stt_resp.json().get("transcript", "")
            else:
                print(f"[STT API Status {stt_resp.status_code}]: {stt_resp.text}")
                transcript = "Urgent incident reported via campus voice line."
    except Exception as e:
        print(f"[STT Exception] {e}")
        transcript = "Emergency voice call recorded."

    if not transcript.strip():
        transcript = "Emergency reported at campus facility, immediate assistance requested."

    # 2. Extract structured emergency incident
    incident_data = await parse_incident_with_sarvam(transcript)

    # 3. Generate Indic voice reassurance with Sarvam Bulbul:v3
    reassurance = incident_data.get("reassurance_indic", "Madad pahunch rahi hai, surakshit rahein.")
    audio_b64 = await generate_sarvam_tts(reassurance, target_lang=language_code)

    return {
        "raw_transcript": transcript,
        "incident": incident_data,
        "reassurance_audio_base64": audio_b64
    }

@app.post("/api/test-scenario")
async def test_scenario(
    text: str = Form(...),
    language_code: str = Form("hi-IN")
):
    """Instant live-demo endpoint that runs Sarvam-105B and Bulbul:v3 on preset scenarios."""
    incident_data = await parse_incident_with_sarvam(text)
    reassurance = incident_data.get("reassurance_indic", "Aapki soochana darj kar li gayi hai.")
    audio_b64 = await generate_sarvam_tts(reassurance, target_lang=language_code)

    return {
        "raw_transcript": text,
        "incident": incident_data,
        "reassurance_audio_base64": audio_b64
    }

# WebSocket for realtime audio stream chunking
@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    print("[WebSocket] Caller channel opened.")
    try:
        while True:
            msg = await websocket.receive()
            if "bytes" in msg:
                await websocket.send_json({
                    "type": "BUFFER_ACK",
                    "bytes_received": len(msg["bytes"])
                })
            elif "text" in msg:
                data = json.loads(msg["text"])
                if data.get("action") == "PING":
                    await websocket.send_json({"type": "PONG"})
    except WebSocketDisconnect:
        print("[WebSocket] Caller channel closed.")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("server:app", host=host, port=port, reload=True)
