import streamlit as st
import pandas as pd
import requests
import os
import re
import io
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ─── Config ───
OAUTH_LOCAL_SERVER_PORT = int(os.getenv("OAUTH_LOCAL_SERVER_PORT", 8080))
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

# Write credentials.json from Streamlit Secrets if running on the cloud
if not os.path.exists(CREDENTIALS_FILE) and "gcp_credentials" in st.secrets:
    creds_data = {
        "installed": {
            "client_id": st.secrets["gcp_credentials"]["client_id"],
            "project_id": st.secrets["gcp_credentials"]["project_id"],
            "auth_uri": st.secrets["gcp_credentials"]["auth_uri"],
            "token_uri": st.secrets["gcp_credentials"]["token_uri"],
            "auth_provider_x509_cert_url": st.secrets["gcp_credentials"]["auth_provider_x509_cert_url"],
            "client_secret": st.secrets["gcp_credentials"]["client_secret"],
            "redirect_uris": list(st.secrets["gcp_credentials"]["redirect_uris"]),
        }
    }
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds_data, f)

# Write token.json from Streamlit Secrets if running on the cloud
if not os.path.exists(TOKEN_FILE) and "gcp_token" in st.secrets:
    token_data = {
        "token": st.secrets["gcp_token"]["token"],
        "refresh_token": st.secrets["gcp_token"]["refresh_token"],
        "token_uri": st.secrets["gcp_token"]["token_uri"],
        "client_id": st.secrets["gcp_token"]["client_id"],
        "client_secret": st.secrets["gcp_token"]["client_secret"],
        "scopes": list(st.secrets["gcp_token"]["scopes"]),
        "universe_domain": st.secrets["gcp_token"]["universe_domain"],
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)

st.set_page_config(
    page_title="Sheet → Drive Uploader",
    page_icon="📤",
    layout="wide",
)

# ─── Custom CSS ───
st.markdown("""
<style>
    .main { padding-top: 1rem; }
    .stProgress > div > div > div > div {
        background: linear-gradient(90deg, #4CAF50, #8BC34A);
    }
    div[data-testid="stMetric"] {
        background: #f8f9fa;
        border-radius: 10px;
        padding: 10px 16px;
        border-left: 4px solid #4CAF50;
    }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════
#  Google Drive Authentication
# ═══════════════════════════════════════════

def get_drive_service():
    """Authenticate and return a Google Drive API service object."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
        else:
            return None  # Cannot open browser on cloud — token must come from Secrets

    return build("drive", "v3", credentials=creds)


def create_drive_folder(service, folder_name, parent_id=None):
    """Create a folder in Google Drive and return its ID."""
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id, webViewLink").execute()
    return folder.get("id"), folder.get("webViewLink")


def upload_to_drive(service, filepath, filename, folder_id):
    """Upload a file to a specific Google Drive folder."""
    mime_map = {
        ".pdf": "application/pdf",
        ".zip": "application/zip",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".txt": "text/plain",
        ".html": "text/html",
        ".json": "application/json",
        ".mp4": "video/mp4",
        ".csv": "text/csv",
        ".py": "text/x-python",
        ".js": "text/javascript",
        ".tar": "application/x-tar",
        ".gz": "application/gzip",
    }
    ext = Path(filename).suffix.lower()
    mime_type = mime_map.get(ext, "application/octet-stream")

    file_metadata = {
        "name": filename,
        "parents": [folder_id],
    }
    media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)
    uploaded = service.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    return uploaded.get("id"), uploaded.get("webViewLink")


# ═══════════════════════════════════════════
#  Google Sheet Helpers
# ═══════════════════════════════════════════

def extract_sheet_id(url: str) -> str | None:
    patterns = [
        r'/spreadsheets/d/([a-zA-Z0-9-_]+)',
        r'id=([a-zA-Z0-9-_]+)',
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def extract_gid(url: str) -> str:
    m = re.search(r'[#&?]gid=(\d+)', url)
    return m.group(1) if m else "0"


def fetch_sheet_as_df(url: str) -> pd.DataFrame:
    sheet_id = extract_sheet_id(url)
    if not sheet_id:
        raise ValueError(
            "Could not extract a valid Sheet ID from the URL. "
            "Make sure the sheet is shared publicly."
        )
    gid = extract_gid(url)
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    resp = requests.get(csv_url, timeout=30)
    if resp.status_code != 200:
        raise ConnectionError(
            f"Failed to fetch sheet (HTTP {resp.status_code}). "
            "Make sure the sheet is publicly accessible (Anyone with the link → Viewer)."
        )
    return pd.read_csv(io.StringIO(resp.text))


# ═══════════════════════════════════════════
#  File Download Helper
# ═══════════════════════════════════════════

def guess_extension(url: str, content_type: str = "") -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext and len(ext) <= 6:
        return ext
    ct_map = {
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "text/plain": ".txt",
        "text/html": ".html",
        "application/json": ".json",
        "application/octet-stream": ".bin",
        "application/x-tar": ".tar",
        "application/gzip": ".gz",
        "video/mp4": ".mp4",
    }
    for key, val in ct_map.items():
        if key in content_type:
            return val
    return ".bin"


def download_file(url: str, user_id: str) -> tuple[str | None, str, str | None]:
    """Download a file. Returns (filepath, status, filename)."""
    try:
        gdrive_match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
        if 'drive.google.com' in url and gdrive_match:
            file_id = gdrive_match.group(1)
            url = f"https://drive.google.com/uc?export=download&id={file_id}"

        resp = requests.get(url, stream=True, timeout=60, allow_redirects=True)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}", None

        content_type = resp.headers.get("Content-Type", "")
        ext = guess_extension(url, content_type)
        safe_name = re.sub(r'[^\w\-.]', '_', str(user_id))
        filename = f"{safe_name}{ext}"
        filepath = DOWNLOAD_DIR / filename

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        size_kb = filepath.stat().st_size / 1024
        return str(filepath), f"{size_kb:.1f} KB", filename

    except requests.exceptions.Timeout:
        return None, "Timeout", None
    except Exception as e:
        return None, str(e), None


# ═══════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════

st.title("📤 Google Sheet → Drive Uploader")
st.caption(
    "Reads your Google Sheet, downloads each file, "
    "and uploads them directly to a new Google Drive folder — fully automated."
)

# ── Sidebar: Auth Status ──
with st.sidebar:
    st.header("🔐 Google Drive Auth")

    if os.path.exists(TOKEN_FILE):
        st.success("✅ Authenticated with Google Drive")
        if st.button("🔄 Re-authenticate"):
            os.remove(TOKEN_FILE)
            st.rerun()
    elif os.path.exists(CREDENTIALS_FILE):
        st.warning("⚠️ Not authenticated yet")
        st.info(
            "Click the button below. A browser window will open "
            "asking you to sign in with Google."
        )
        if st.button("🔑 Authenticate with Google", type="primary"):
            with st.spinner("Opening browser for authentication..."):
                service = get_drive_service()
                if service:
                    st.success("✅ Authenticated successfully!")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("Authentication failed.")
    else:
        st.error(
            "❌ `credentials.json` not found!\n\n"
            "Place your Google OAuth credentials file "
            "in the same folder as this app."
        )

    st.divider()
    st.header("⚙️ Settings")
    folder_name = st.text_input(
        "Drive folder name",
        value=f"Submissions_{time.strftime('%Y%m%d_%H%M')}",
        help="A new folder with this name will be created in your Drive root."
    )

# ── Step 1: Google Sheet URL ──
st.header("① Paste your Google Sheet link")
sheet_url = st.text_input(
    "Google Sheet URL",
    placeholder="https://docs.google.com/spreadsheets/d/1aBc.../edit#gid=0",
    help="Sheet must be shared as **Anyone with the link → Viewer**",
)

col1, col2 = st.columns(2)
with col1:
    col_user = st.text_input("User ID column", value="user_id")
with col2:
    col_url = st.text_input("Download URL column", value="code_submission_url")

# ── Step 2: Load & Preview ──
if sheet_url:
    with st.spinner("Fetching sheet…"):
        try:
            df = fetch_sheet_as_df(sheet_url)
            st.success(f"Loaded **{len(df)} rows** × **{len(df.columns)} columns**")
        except Exception as e:
            st.error(str(e))
            st.stop()

    missing = [c for c in [col_user, col_url] if c not in df.columns]
    if missing:
        st.error(
            f"Column(s) not found: **{', '.join(missing)}**. "
            f"Available: {', '.join(df.columns)}"
        )
        st.stop()

    df_clean = df[[col_user, col_url]].dropna()
    skipped = len(df) - len(df_clean)
    if skipped:
        st.warning(f"Skipped **{skipped}** rows with missing data.")

    st.subheader("Preview")
    st.dataframe(df_clean.head(10), use_container_width=True)

    # ── Step 3: Download & Upload ──
    st.header("② Download & Upload to Drive")

    auth_ready = os.path.exists(TOKEN_FILE)
    if not auth_ready:
        st.warning("👈 Please authenticate with Google Drive in the sidebar first.")

    if st.button(
        f"🚀 Download & Upload {len(df_clean)} files to Drive",
        type="primary",
        use_container_width=True,
        disabled=not auth_ready,
    ):
        # Authenticate
        service = get_drive_service()
        if not service:
            st.error("Failed to connect to Google Drive. Please re-authenticate.")
            st.stop()

        # Create folder
        with st.spinner(f"Creating Drive folder: **{folder_name}**"):
            folder_id, folder_link = create_drive_folder(service, folder_name)
            st.success(f"📁 Created folder: [{folder_name}]({folder_link})")

        # Process each row
        results = []
        progress = st.progress(0, text="Starting…")

        total = len(df_clean)
        for i, (_, row) in enumerate(df_clean.iterrows()):
            uid = str(row[col_user])
            url = str(row[col_url])

            progress.progress(
                (i + 1) / total,
                text=f"Processing {i+1}/{total} — {uid}"
            )

            # Download
            filepath, dl_status, filename = download_file(url, uid)

            if filepath and filename:
                # Upload to Drive
                try:
                    file_id, file_link = upload_to_drive(
                        service, filepath, filename, folder_id
                    )
                    results.append({
                        "user_id": uid,
                        "filename": filename,
                        "size": dl_status,
                        "status": "✅ Uploaded",
                        "drive_link": file_link,
                    })
                except Exception as e:
                    results.append({
                        "user_id": uid,
                        "filename": filename,
                        "size": dl_status,
                        "status": f"❌ Upload failed: {e}",
                        "drive_link": "",
                    })

                # Clean up local file
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            else:
                results.append({
                    "user_id": uid,
                    "filename": "—",
                    "size": "—",
                    "status": f"❌ Download failed: {dl_status}",
                    "drive_link": "",
                })

        progress.progress(1.0, text="✅ All done!")

        # ── Step 4: Results ──
        st.header("③ Results")
        result_df = pd.DataFrame(results)

        successes = result_df[result_df["status"].str.startswith("✅")]
        failures = result_df[~result_df["status"].str.startswith("✅")]

        c1, c2, c3 = st.columns(3)
        c1.metric("Total", len(result_df))
        c2.metric("Uploaded ✅", len(successes))
        c3.metric("Failed ❌", len(failures))

        if not folder_link:
            folder_link = "#"
        st.markdown(f"### 📁 [Open Drive Folder]({folder_link})")

        if not failures.empty:
            with st.expander(f"⚠️ {len(failures)} Failed", expanded=True):
                st.dataframe(
                    failures[["user_id", "status"]],
                    use_container_width=True,
                )

        if not successes.empty:
            with st.expander(f"✅ {len(successes)} Uploaded Successfully", expanded=True):
                st.dataframe(
                    successes[["user_id", "filename", "size", "drive_link"]],
                    use_container_width=True,
                )
