
import os
import json
import uuid
import secrets
import hmac
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from flask import (
    Flask,
    render_template,
    request,
    make_response,
    jsonify,
    session,
    redirect
)

app = Flask(__name__)




# --------------------------------------------------
# Load environment variables
# --------------------------------------------------

def load_env():
    env_path = Path(__file__).parent / ".env"

    # On Vercel, environment variables are provided by the platform.
    if os.environ.get("VERCEL"):
        return

    # Locally, load environment variables from .env.
    if not env_path.exists():
        raise RuntimeError(".env file not found")

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()

app.secret_key = os.environ["FLASK_SECRET_KEY"]
PRESENTER_PASSWORD = os.environ["PRESENTER_PASSWORD"]

limiter = Limiter(
    get_remote_address,
    app=app,
    storage_uri=os.environ["REDIS_URL"],
    default_limits=[]
)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]



# --------------------------------------------------
# Supabase helpers
# --------------------------------------------------

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        
    }


def supabase_get(table, query):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"

    req = Request(
        url,
        headers=supabase_headers(),
        method="GET"
    )

    with urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def supabase_insert(table, data, prefer="return=minimal"):
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    payload = json.dumps(data).encode("utf-8")

    headers = supabase_headers()
    headers.update({
        "Content-Type": "application/json",
        "Prefer": prefer,
    })

    req = Request(
        url,
        data=payload,
        headers=headers,
        method="POST"
    )

    with urlopen(req, timeout=10) as response:
        return response.read()

def supabase_rpc(function_name, data=None):
    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"

    payload = json.dumps(data or {}).encode("utf-8")

    headers = supabase_headers()
    headers["Content-Type"] = "application/json"

    req = Request(
        url,
        data=payload,
        headers=headers,
        method="POST"
    )

    try:
        with urlopen(req, timeout=10) as response:
            return response.read()

    except HTTPError as error:
        error_details = error.read().decode(
            "utf-8",
            errors="replace"
        )

        app.logger.error(
            "Supabase RPC error (%s): %s",
            error.code,
            error_details
        )

        raise



def supabase_delete(table, query):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"

    headers = supabase_headers()
    headers["Prefer"] = "return=minimal"

    req = Request(
        url,
        headers=headers,
        method="DELETE"
    )

    with urlopen(req, timeout=10) as response:
        return response.read()

# --------------------------------------------------
# Detect English or Japanese writing
# --------------------------------------------------

def detect_language(text):
    has_japanese = any(
        "\u3040" <= char <= "\u30ff"
        or "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        for char in text
    )

    has_latin = any(
        "a" <= char.lower() <= "z"
        for char in text
    )

    has_kana = any(
        "\u3040" <= char <= "\u30ff"
        for char in text
    )

    if has_kana:
        return "ja"

    if has_latin and not has_japanese:
        return "en"

    return None


# --------------------------------------------------
# Retrieve questions
# --------------------------------------------------


def get_questions():
    # Retrieve all questions.
    questions = supabase_get(
        "questions",
        "select=id,original_text,original_language,"
        "translated_text,translation_status,created_at"
        "&order=created_at.desc"
    )

    # Retrieve likes for all questions.
    likes = supabase_get(
        "question_likes",
        "select=question_id"
    )

    # Count likes for each question.
    like_counts = {}

    for like in likes:
        question_id = like["question_id"]

        like_counts[question_id] = (
            like_counts.get(question_id, 0) + 1
        )

    # Add the like count to each question.
    for question in questions:
        question["like_count"] = like_counts.get(
            question["id"],
            0
        )

    # Sort by most likes first.
    questions.sort(
        key=lambda question: question["like_count"],
        reverse=True
    )

    return questions



# --------------------------------------------------
# Participant question feed
# --------------------------------------------------

@app.route("/participant/questions")
def participant_questions():
    participant_id = request.args.get("participant_id", "").strip()

    if not participant_id or len(participant_id) > 100:
        return jsonify({
            "error": "Invalid participant ID"
        }), 400

    try:
        questions = get_questions()

        likes = supabase_get(
            "question_likes",
            "select=question_id,participant_id"
        )

        # Calculate likes and participant-specific state.
        like_counts = {}
        liked_questions = set()

        for like in likes:
            question_id = like["question_id"]

            like_counts[question_id] = (
                like_counts.get(question_id, 0) + 1
            )

            if like["participant_id"] == participant_id:
                liked_questions.add(question_id)

        # Add voting information to each question.
        for question in questions:
            question_id = question["id"]

            question["like_count"] = like_counts.get(
                question_id, 0
            )

            question["liked_by_me"] = (
                question_id in liked_questions
            )

        # Popular sorting: highest likes first.
        popular = sorted(
            questions,
            key=lambda question: (
                -question["like_count"],
                question["created_at"]
            )
        )

        # Newest sorting: latest questions first.
        newest = sorted(
            questions,
            key=lambda question: question["created_at"],
            reverse=True
        )

        return jsonify({
            "popular": popular,
            "newest": newest
        })

    except (HTTPError, URLError, TimeoutError):
        app.logger.exception(
            "Unable to retrieve participant questions"
        )

        return jsonify({
            "error": "Unable to load questions"
        }), 502


# --------------------------------------------------
# Like a question
# --------------------------------------------------

@app.route("/like-question", methods=["POST"])



@app.route("/like-question", methods=["POST"])
def like_question():
    data = request.get_json(silent=True) or {}

    question_id = str(data.get("question_id", "")).strip()
    participant_id = str(data.get("participant_id", "")).strip()

    if not question_id or not participant_id:
        return jsonify({
            "error": "Question ID and participant ID are required"
        }), 400

    # Validate participant ID format.
    try:
        participant_id = str(uuid.UUID(participant_id))
    except ValueError:
        return jsonify({
            "error": "Invalid participant ID"
        }), 400

    try:
        supabase_insert(
            "question_likes",
            {
                "question_id": question_id,
                "participant_id": participant_id
            }
        )

        return jsonify({
            "success": True,
            "liked": True
        }), 201

    except HTTPError as error:
        # Database unique constraint prevents duplicate likes.
        if error.code == 409:
            return jsonify({
                "success": True,
                "liked": True,
                "already_liked": True
            }), 200

        app.logger.error(
            "Like insertion failed: HTTP %s",
            error.code
        )

        return jsonify({
            "error": "Unable to like this question"
        }), 502

    except (URLError, TimeoutError):
        app.logger.exception(
            "Supabase connection failed while liking question"
        )

        return jsonify({
            "error": "Unable to like this question"
        }), 502



# --------------------------------------------------
# Participant page
# --------------------------------------------------

@app.route("/")
def home():
    return redirect("/presentation")


@app.route("/join")
def join():
    return render_template("index.html")

# --------------------------------------------------
# Protected new speaker session
# --------------------------------------------------

@app.route("/presentation/reset", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def reset_presentation():

    # Generate a CSRF token when opening the reset page.
    if request.method == "GET":
        session["reset_csrf_token"] = secrets.token_urlsafe(32)

        return render_template(
            "reset.html",
            csrf_token=session["reset_csrf_token"]
        )

    # Verify the CSRF token.
    submitted_token = request.form.get("csrf_token", "")
    stored_token = session.get("reset_csrf_token", "")

    if not stored_token or not hmac.compare_digest(
        submitted_token,
        stored_token
    ):
        return "Invalid security token. Please try again.", 403

    # Verify the presenter password.
    password = request.form.get("password", "")

    if not hmac.compare_digest(password, PRESENTER_PASSWORD):
        return render_template(
            "reset.html",
            csrf_token=stored_token,
            error="Incorrect password. Please try again."
        ), 401

    # Delete previous session data.
    try:
       supabase_rpc("reset_presentation_session")

       session.pop("reset_csrf_token", None)

       return redirect("/presentation")

    except (HTTPError, URLError, TimeoutError):
        app.logger.exception(
            "Unable to reset the previous presentation"
        )

        return render_template(
            "reset.html",
            csrf_token=stored_token,
            error="Unable to reset the session. Please try again."
        ), 502

# --------------------------------------------------
# Presenter QR welcome screen
# --------------------------------------------------

@app.route("/presentation")
def presentation():
    return render_template("qr.html")


# --------------------------------------------------
# Submitted questions screen
# --------------------------------------------------

@app.route("/presentation/live")
def presentation_live():
    try:
        questions = get_questions()

        return render_template(
            "presentation.html",
            questions=questions
        )

    except (HTTPError, URLError, TimeoutError):
        app.logger.exception(
            "Unable to retrieve questions from Supabase"
        )

        return "Unable to load questions. Please try again.", 502


# --------------------------------------------------
# Refresh questions for HTMX
# --------------------------------------------------

@app.route("/presentation/questions")
def presentation_questions():
    try:
        questions = get_questions()

        return render_template(
            "_question_list.html",
            questions=questions
        )

    except (HTTPError, URLError, TimeoutError):
        app.logger.exception(
            "Unable to refresh questions"
        )

        return "", 502


# --------------------------------------------------
# Submit a question
# --------------------------------------------------

@app.route("/submit-question", methods=["POST"])
def submit_question():

    question = request.form.get("question", "").strip()
    language = request.form.get("language", "").strip()
    ui_language = request.form.get("ui_language", "en").strip()

    # Validate submission.
    if not question:
        return '<p role="alert">Please enter a question.</p>', 400

    if len(question) > 2000:
        return '<p role="alert">Your question is too long.</p>', 400

    if language not in ("en", "ja"):
        return '<p role="alert">Please select a valid language.</p>', 400

    # Check whether the written language matches the selection.
    detected_language = detect_language(question)

    if detected_language != language:
        return """
            <div id="language-confirmation" role="status">
                <div class="confirmation-text">
                    <div data-i18n="languageWarningTitle"></div>
                    <div data-i18n="languageWarningDescription"></div>
                </div>

                <div class="confirmation-buttons">
                    <button type="button"
                        onclick="confirmQuestionLanguage('en')">
                        English
                    </button>

                    <button type="button"
                        class="secondary"
                        onclick="confirmQuestionLanguage('ja')">
                        日本語
                    </button>
                </div>
            </div>
        """

    # Prepare database record.
    data = {
        "original_text": question,
        "original_language": language,
        "translation_status": "pending"
    }

    try:
        supabase_insert("questions", data)

        # Return confirmation in the selected language.
        if ui_language == "ja":
            message = "✓ 質問が正常に送信されました！"
        else:
            message = "✓ Your question has been submitted successfully!"

        response = make_response(
               f"""
               <div id="submission-confirmation" role="status">
              {message}
               </div>
           """
        )

        response.headers["X-Submission-Saved"] = "true"

        return response

    except HTTPError as error:
        app.logger.error(
            "Supabase insertion failed: HTTP %s",
            error.code
        )

        return (
            '<p role="alert">Unable to submit your question. Please try again.</p>',
            502
        )

    except (URLError, TimeoutError):
        app.logger.exception(
            "Supabase connection or insertion failed"
        )

        return (
            '<p role="alert">Unable to submit your question. Please try again.</p>',
            502
        )


# --------------------------------------------------
# Start Flask
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)



