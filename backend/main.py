from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, status, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import StreamingResponse, FileResponse
from datetime import datetime, timedelta
from typing import List, Optional, Dict
import json
import os
import tempfile
from pathlib import Path
from jose import JWTError, jwt
from passlib.context import CryptContext
from bson import ObjectId
from pydantic import BaseModel, Field
import logging
import io

from voice_analyzer import VoiceAnalyzer
from enhanced_analyzer import EnhancedAnalyzer
from export_utils import DataExporter, ChartGenerator
from database import (
    init_db,
    create_user,
    get_user_by_username,
    get_user_by_email,
    create_recording,
    get_user_recordings,
    create_practice_session,
    get_user_practice_sessions,
    update_progress_metrics,
    get_user_progress
)
from models import (
    UserDB, RecordingDB, PracticeSessionDB, ProgressMetricsDB,
    LanguageDetectionResult, VoiceCloningDetectionResult, ExportRequest
)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI App
app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://localhost:5176",
        "http://localhost:5177",
        "http://localhost:5178",
        "http://localhost:5179",
        "http://localhost:5180",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://127.0.0.1:5176",
        "http://127.0.0.1:5177",
        "http://127.0.0.1:5178",
        "http://127.0.0.1:5179",
        "http://127.0.0.1:5180",
        "http://127.0.0.1:5500",  # Live Server
        "http://localhost:5500",   # Live Server alternative
        "*"  # Allow all origins for testing
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# JWT Setup
SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key")
logger.info(f"Using SECRET_KEY: {SECRET_KEY}")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Auth
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Request Models
class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

# Auth Helpers
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    user = await get_user_by_username(token_data.username)
    if user is None:
        raise credentials_exception
    return user

# Startup: init DB
@app.on_event("startup")
async def startup_event():
    logger.info("Initializing database...")
    await init_db()

# Routes
@app.post("/register", response_model=Token)
async def register_user_endpoint(user: UserCreate):
    if await get_user_by_username(user.username):
        raise HTTPException(status_code=400, detail="Username already registered")
    if await get_user_by_email(user.email):
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = get_password_hash(user.password)
    user_db = UserDB(username=user.username, email=user.email, hashed_password=hashed_password)
    await create_user(user_db)

    access_token = create_access_token(data={"sub": user.username}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/token", response_model=Token)
async def login_endpoint(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await get_user_by_username(form_data.username)
    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": user["username"]}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/users/me", response_model=UserDB)
async def read_users_me(current_user: dict = Depends(get_current_user)):
    return current_user

@app.get("/test")
async def test_endpoint():
    return {"message": "Backend is running and reachable!"}

@app.get("/test-analyzer")
async def test_analyzer_endpoint():
    try:
        # Test if voice analyzer can be initialized
        test_analyzer = VoiceAnalyzer()
        return {"message": "Voice analyzer initialized successfully!", "status": "ok"}
    except Exception as e:
        logger.error(f"Voice analyzer initialization failed: {str(e)}")
        return {"message": f"Voice analyzer initialization failed: {str(e)}", "status": "error"}

voice_analyzer = VoiceAnalyzer()
enhanced_analyzer = EnhancedAnalyzer()
data_exporter = DataExporter()
chart_generator = ChartGenerator()

@app.post("/test-analyze-audio")
async def test_analyze_audio_endpoint(
    file: UploadFile = File(...),
    session_type: str = Form("practice"),
    topic: str = Form("general")
):
    """Test endpoint for audio analysis without authentication"""
    temp_file_path = None
    try:
        logger.info(f"Received TEST audio analysis request")
        logger.info(f"File: {file.filename}, Size: {file.size}, Content-Type: {file.content_type}")
        logger.info(f"Session type: {session_type}, Topic: {topic}")
        
        # Validate file
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided")
        
        if file.size == 0:
            raise HTTPException(status_code=400, detail="Empty file provided")
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_file_path = temp_file.name
            logger.info(f"Created temporary file: {temp_file_path}")

        # Analyze audio
        logger.info("Starting audio analysis...")
        analysis_results = await voice_analyzer.analyze_audio(temp_file_path)
        logger.info("Audio analysis completed successfully")

        return {"id": "test", **analysis_results}
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error in test_analyze_audio_endpoint: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        # Clean up temporary file
        if temp_file_path and Path(temp_file_path).exists():
            try:
                Path(temp_file_path).unlink()
                logger.info(f"Cleaned up temporary file: {temp_file_path}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up temporary file {temp_file_path}: {str(cleanup_error)}")

@app.post("/analyze-audio")
async def analyze_audio_endpoint(
    file: UploadFile = File(...),
    session_type: str = Form("practice"),
    topic: str = Form("general"),
    current_user: dict = Depends(get_current_user)
):
    temp_file_path = None
    try:
        logger.info(f"Received audio analysis request from user: {current_user['username']}")
        logger.info(f"File: {file.filename}, Size: {file.size}, Content-Type: {file.content_type}")
        logger.info(f"Session type: {session_type}, Topic: {topic}")
        
        # Validate file
        if not file.filename:
            raise HTTPException(status_code=400, detail="No file provided")
        
        if file.size == 0:
            raise HTTPException(status_code=400, detail="Empty file provided")
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_file_path = temp_file.name
            logger.info(f"Created temporary file: {temp_file_path}")

        # Analyze audio
        logger.info("Starting audio analysis...")
        analysis_results = await voice_analyzer.analyze_audio(temp_file_path)
        logger.info("Audio analysis completed successfully")

        # Save to database
        try:
            user_id = ObjectId(current_user["_id"])
            recording_db = RecordingDB(
                user_id=user_id,
                file_path=temp_file_path,
                session_type=session_type,
                topic=topic,
                analysis_result=analysis_results,
                analysis_summary=json.dumps(analysis_results, indent=2)
            )
            recording_id = await create_recording(recording_db)
            logger.info(f"Recording saved to database with ID: {recording_id}")

            session_db = PracticeSessionDB(
                user_id=user_id,
                recording_id=ObjectId(recording_id),
                session_type=session_type,
                topic=topic,
                analysis_result=analysis_results,
            )
            await create_practice_session(session_db)
            logger.info("Practice session saved to database")

        except Exception as db_error:
            logger.error(f"Database error: {str(db_error)}")
            # Don't fail the request if database save fails
            logger.warning("Continuing with analysis results despite database error")

        return {"id": recording_id if 'recording_id' in locals() else "temp", **analysis_results}
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error in analyze_audio_endpoint: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        # Clean up temporary file
        if temp_file_path and Path(temp_file_path).exists():
            try:
                Path(temp_file_path).unlink()
                logger.info(f"Cleaned up temporary file: {temp_file_path}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean up temporary file {temp_file_path}: {str(cleanup_error)}")

@app.get("/user/progress")
async def get_user_progress_endpoint(current_user: dict = Depends(get_current_user)):
    print("DEBUG: /user/progress endpoint called")
    user_id = ObjectId(current_user["_id"])
    recordings_list = await get_user_recordings(user_id)

    if not recordings_list:
        return {"total_recordings": 0, "latest_metrics": None}

    clarity_scores = []
    confidence_scores = []
    speech_rates = []

    for r in recordings_list:
        analysis_result = r.get("analysis_result", {})
        audio_metrics = analysis_result.get("audio_metrics", {})

        clarity_data = audio_metrics.get("clarity", {})
        if "clarity_score" in clarity_data:
            clarity_scores.append(clarity_data["clarity_score"])

        emotion_data = audio_metrics.get("emotion", {})
        if "emotion_confidence" in emotion_data:
            confidence_scores.append(emotion_data["emotion_confidence"])

        rhythm_data = audio_metrics.get("rhythm", {})
        if "speech_rate" in rhythm_data:
            speech_rates.append(rhythm_data["speech_rate"])

    metrics_data = ProgressMetricsDB(
        user_id=user_id,
        clarity_trend=sum(clarity_scores) / len(clarity_scores) if clarity_scores else 0,
        confidence_trend=sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0,
        speech_rate_trend=sum(speech_rates) / len(speech_rates) if speech_rates else 0,
        emotion_expression_score=sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0,
        vocabulary_score=0.0,
        overall_improvement=calculate_overall_improvement(recordings_list),
        current_goals=[],
        completed_goals=[],
        badges_earned=[]
    )

    await update_progress_metrics(metrics_data)

    return {
        "total_recordings": len(recordings_list),
        "latest_metrics": metrics_data.model_dump(by_alias=True)
    }

@app.get("/practice-sessions")
async def get_practice_sessions_endpoint(current_user: dict = Depends(get_current_user)):
    return await get_user_practice_sessions(ObjectId(current_user["_id"]))

@app.get("/user/recordings")
async def get_user_recordings_endpoint(current_user: dict = Depends(get_current_user)):
    recordings_list = await get_user_recordings(ObjectId(current_user["_id"]))

    if not recordings_list:
        return {
            "message": "No recordings found",
            "recordings": []
        }

    formatted_recordings = [
        {
            "id": str(r["_id"]),
            "file_path": r["file_path"],
            "session_type": r["session_type"],
            "topic": r["topic"],
            "analysis_result": r["analysis_result"],
            "analysis_summary": r["analysis_summary"],
            "created_at": r["created_at"].isoformat()
        }
        for r in recordings_list
    ]

    return {
        "total_recordings": len(formatted_recordings),
        "recordings": formatted_recordings
    }

def calculate_overall_improvement(recordings: List[Dict]) -> float:
    if len(recordings) < 2:
        return 0.0

    first_result = recordings[-1].get("analysis_result", {})
    latest_result = recordings[0].get("analysis_result", {})

    first_audio_metrics = first_result.get("audio_metrics", {})
    latest_audio_metrics = latest_result.get("audio_metrics", {})

    first_clarity = first_audio_metrics.get("clarity", {}).get("clarity_score", 0.0)
    latest_clarity = latest_audio_metrics.get("clarity", {}).get("clarity_score", 0.0)

    first_confidence = first_audio_metrics.get("emotion", {}).get("emotion_confidence", 0.0)
    latest_confidence = latest_audio_metrics.get("emotion", {}).get("emotion_confidence", 0.0)

    first_speech_rate = first_audio_metrics.get("rhythm", {}).get("speech_rate", 0.0)
    latest_speech_rate = latest_audio_metrics.get("rhythm", {}).get("speech_rate", 0.0)

    clarity_improvement = latest_clarity - first_clarity
    confidence_improvement = latest_confidence - first_confidence
    rhythm_improvement = first_speech_rate - latest_speech_rate

    weighted_improvement = (
        clarity_improvement * 0.4 +
        confidence_improvement * 0.3 +
        rhythm_improvement * 0.3
    )

    return max(0.0, min(1.0, weighted_improvement + 0.5))

@app.get("/test-db")
async def test_db_endpoint():
    """Test database connection"""
    try:
        # Test database connection
        await init_db()
        return {"message": "Database connection successful!", "status": "ok"}
    except Exception as e:
        logger.error(f"Database connection failed: {str(e)}")
        return {"message": f"Database connection failed: {str(e)}", "status": "error"}

# Enhanced Analysis Endpoints
@app.post("/analyze-audio-enhanced")
async def analyze_audio_enhanced_endpoint(
    file: UploadFile = File(...),
    session_type: str = Form("practice"),
    topic: str = Form("general"),
    current_user: dict = Depends(get_current_user)
):
    """Enhanced audio analysis with language detection and voice cloning detection"""
    temp_file_path = None
    try:
        logger.info(f"Received enhanced audio analysis request from user: {current_user['username']}")
        
        # Validate file
        if not file.filename or file.size == 0:
            raise HTTPException(status_code=400, detail="Invalid file provided")
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_file_path = temp_file.name
        
        # Perform enhanced analysis
        enhanced_results = enhanced_analyzer.analyze_audio_enhanced(temp_file_path)
        
        # Log language detection results for debugging
        language_detection = enhanced_results.get("language_detection", {})
        logger.info(f"Language detection result: {language_detection.get('detected_language', 'unknown')} "
                   f"({language_detection.get('language_name', 'Unknown')}) "
                   f"with confidence: {language_detection.get('confidence', 0):.2f}")
        
        if language_detection.get('detection_features'):
            features = language_detection['detection_features']
            logger.info(f"Detection features - Centroid: {features.get('spectral_centroid', 0):.2f}, "
                       f"Rolloff: {features.get('spectral_rolloff', 0):.2f}, "
                       f"ZCR: {features.get('zero_crossing_rate', 0):.4f}, "
                       f"MFCC_std: {features.get('mfcc_std', 0):.2f}")
            logger.info(f"Language scores - Telugu: {features.get('telugu_score', 0)}, "
                       f"Kannada: {features.get('kannada_score', 0)}, "
                       f"Hindi: {features.get('hindi_score', 0)}")
        
        # Perform regular voice analysis
        voice_analysis = await voice_analyzer.analyze_audio(temp_file_path)
        
        # Combine results
        combined_results = {
            **voice_analysis,
            "enhanced_features": enhanced_results
        }
        
        # Save to database with enhanced data
        try:
            user_id = ObjectId(current_user["_id"])
            recording_db = RecordingDB(
                user_id=user_id,
                file_path=temp_file_path,
                session_type=session_type,
                topic=topic,
                analysis_result=combined_results,
                analysis_summary=json.dumps(combined_results, indent=2),
                detected_language=enhanced_results.get("language_detection", {}).get("detected_language"),
                voice_cloning_score=enhanced_results.get("voice_cloning_detection", {}).get("confidence_score"),
                transcription=enhanced_results.get("language_detection", {}).get("transcription")
            )
            recording_id = await create_recording(recording_db)
            
            session_db = PracticeSessionDB(
                user_id=user_id,
                recording_id=ObjectId(recording_id),
                session_type=session_type,
                topic=topic,
                analysis_result=combined_results,
                language=enhanced_results.get("language_detection", {}).get("detected_language"),
                voice_cloning_detected="ai" if enhanced_results.get("voice_cloning_detection", {}).get("is_ai_generated") else "human"
            )
            await create_practice_session(session_db)
            
        except Exception as db_error:
            logger.error(f"Database error: {str(db_error)}")
        
        return {
            "id": recording_id if 'recording_id' in locals() else "temp",
            **combined_results
        }
        
    except Exception as e:
        logger.error(f"Enhanced analysis error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Enhanced analysis failed: {str(e)}")
    finally:
        if temp_file_path and Path(temp_file_path).exists():
            try:
                Path(temp_file_path).unlink()
            except Exception:
                pass

@app.post("/export-data")
async def export_data_endpoint(
    export_request: ExportRequest,
    current_user: dict = Depends(get_current_user)
):
    """Export analysis data to PDF or CSV format"""
    try:
        user_id = ObjectId(current_user["_id"])
        recordings = await get_user_recordings(user_id)
        
        if not recordings:
            raise HTTPException(status_code=404, detail="No recordings found to export")
        
        if export_request.format.lower() == "csv":
            csv_content = data_exporter.export_to_csv(recordings, export_request.model_dump())
            
            # Generate filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"vocal_iq_analysis_{timestamp}.csv"
            
            return StreamingResponse(
                io.StringIO(csv_content),
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
        
        elif export_request.format.lower() == "pdf":
            pdf_content = data_exporter.export_to_pdf(recordings, current_user, export_request.model_dump())
            
            # Generate filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"vocal_iq_analysis_{timestamp}.pdf"
            
            return StreamingResponse(
                io.BytesIO(pdf_content),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
        
        else:
            raise HTTPException(status_code=400, detail="Unsupported export format. Use 'csv' or 'pdf'")
            
    except Exception as e:
        logger.error(f"Export error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

@app.get("/debug-charts")
async def debug_charts_endpoint(current_user: dict = Depends(get_current_user)):
    """Debug endpoint for chart generation"""
    try:
        user_id = ObjectId(current_user["_id"])
        recordings = await get_user_recordings(user_id)
        
        debug_info = {
            "user_id": str(user_id),
            "username": current_user["username"],
            "recordings_count": len(recordings) if recordings else 0,
            "has_recordings": bool(recordings),
            "chart_generator_initialized": hasattr(chart_generator, 'generate_comparison_chart')
        }
        
        if recordings:
            # Test chart generation
            try:
                chart_data = chart_generator.generate_comparison_chart(recordings)
                debug_info["chart_generation_success"] = True
                debug_info["chart_data_size"] = len(chart_data)
            except Exception as chart_error:
                debug_info["chart_generation_success"] = False
                debug_info["chart_error"] = str(chart_error)
        
        return debug_info
        
    except Exception as e:
        logger.error(f"Debug charts error: {str(e)}")
        return {"error": str(e), "traceback": str(e.__traceback__)}

@app.get("/comparison-charts")
async def get_comparison_charts_endpoint(current_user: dict = Depends(get_current_user)):
    """Generate comparison charts for user's recordings"""
    try:
        user_id = ObjectId(current_user["_id"])
        recordings = await get_user_recordings(user_id)
        
        if not recordings:
            # Return a simple text response instead of 404
            return StreamingResponse(
                io.BytesIO(b"No recordings found. Please record some audio first."),
                media_type="text/plain",
                headers={"Content-Disposition": "inline; filename=no_data.txt"}
            )
        
        # Generate comparison chart
        chart_data = chart_generator.generate_comparison_chart(recordings)
        
        if not chart_data:
            # Return a simple text response instead of 500
            return StreamingResponse(
                io.BytesIO(b"Failed to generate chart. Please try again."),
                media_type="text/plain",
                headers={"Content-Disposition": "inline; filename=error.txt"}
            )
        
        return StreamingResponse(
            io.BytesIO(chart_data),
            media_type="text/plain",  # Changed from image/png to text/plain
            headers={"Content-Disposition": "inline; filename=comparison_chart.txt"}
        )
        
    except Exception as e:
        logger.error(f"Chart generation error: {str(e)}")
        # Return error response instead of raising exception
        return StreamingResponse(
            io.BytesIO(f"Chart generation failed: {str(e)}".encode()),
            media_type="text/plain",
            headers={"Content-Disposition": "inline; filename=error.txt"}
        )

@app.get("/language-charts")
async def get_language_charts_endpoint(current_user: dict = Depends(get_current_user)):
    """Generate language distribution charts"""
    try:
        user_id = ObjectId(current_user["_id"])
        recordings = await get_user_recordings(user_id)
        
        if not recordings:
            # Return a simple text response instead of 404
            return StreamingResponse(
                io.BytesIO(b"No recordings found. Please record some audio first."),
                media_type="text/plain",
                headers={"Content-Disposition": "inline; filename=no_data.txt"}
            )
        
        # Generate language chart
        chart_data = chart_generator.generate_language_chart(recordings)
        
        if not chart_data:
            # Return a simple text response instead of 500
            return StreamingResponse(
                io.BytesIO(b"Failed to generate language chart. Please try again."),
                media_type="text/plain",
                headers={"Content-Disposition": "inline; filename=error.txt"}
            )
        
        return StreamingResponse(
            io.BytesIO(chart_data),
            media_type="text/plain",  # Changed from image/png to text/plain
            headers={"Content-Disposition": "inline; filename=language_chart.txt"}
        )
        
    except Exception as e:
        logger.error(f"Language chart generation error: {str(e)}")
        # Return error response instead of raising exception
        return StreamingResponse(
            io.BytesIO(f"Language chart generation failed: {str(e)}".encode()),
            media_type="text/plain",
            headers={"Content-Disposition": "inline; filename=error.txt"}
        )

@app.get("/supported-languages")
async def get_supported_languages_endpoint():
    """Get list of supported languages for analysis"""
    try:
        languages = {
            'en': 'English',
            'es': 'Spanish', 
            'fr': 'French',
            'de': 'German',
            'it': 'Italian',
            'pt': 'Portuguese',
            'ru': 'Russian',
            'ja': 'Japanese',
            'ko': 'Korean',
            'zh': 'Chinese',
            'ar': 'Arabic',
            'hi': 'Hindi',
            'kn': 'Kannada',
            'te': 'Telugu'
        }
        
        return {
            "supported_languages": languages,
            "total_languages": len(languages),
            "default_language": "en"
        }
        
    except Exception as e:
        logger.error(f"Language list error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get language list: {str(e)}")

@app.get("/favicon.ico")
async def favicon():
    """Return empty favicon to prevent 404 errors"""
    from fastapi.responses import Response
    return Response(status_code=204)

@app.get("/debug-language-detection")
async def debug_language_detection_endpoint(current_user: dict = Depends(get_current_user)):
    """Debug endpoint for language detection"""
    try:
        user_id = ObjectId(current_user["_id"])
        recordings = await get_user_recordings(user_id)
        
        debug_info = {
            "user_id": str(user_id),
            "username": current_user["username"],
            "recordings_count": len(recordings) if recordings else 0,
            "enhanced_analyzer_initialized": hasattr(enhanced_analyzer, 'language_detector'),
            "supported_languages": enhanced_analyzer.language_detector.supported_languages if hasattr(enhanced_analyzer, 'language_detector') else {}
        }
        
        if recordings:
            # Get the most recent recording
            latest_recording = max(recordings, key=lambda x: x.get('created_at', datetime.now()))
            debug_info["latest_recording"] = {
                "id": str(latest_recording.get('_id', '')),
                "created_at": latest_recording.get('created_at', ''),
                "detected_language": latest_recording.get('detected_language', ''),
                "language_confidence": latest_recording.get('language_confidence', 0),
                "transcription": latest_recording.get('transcription', '')[:100] + "..." if latest_recording.get('transcription') else ""
            }
        
        return debug_info
        
    except Exception as e:
        logger.error(f"Debug language detection error: {str(e)}")
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
