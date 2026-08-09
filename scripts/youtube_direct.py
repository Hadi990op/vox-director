#!/usr/bin/env python3
"""
YouTube Upload via Google Data API v3 (Direct)
==============================================
Uploads a generated video directly to YouTube using Google's YouTube Data API v3.
No third-party (Composio) needed — uses Google OAuth 2.0 directly.

First-time setup:
  1. Go to https://console.cloud.google.com/
  2. Create a project (or use existing)
  3. Enable "YouTube Data API v3"
  4. Create OAuth 2.0 credentials (Desktop app type)
  5. Download client_secret.json and save to vox-director/client_secret.json
  6. Run: python3 youtube_direct.py --auth
     → Opens browser, user authorizes, saves token to .youtube_token.json
  7. After that, uploads work automatically using the saved token

Usage:
  python3 youtube_direct.py <project_dir> [--title "..."] [--description "..."] [--tags tag1,tag2]
  python3 youtube_direct.py --auth  # One-time authorization
"""
import argparse
import json
import os
import sys
from pathlib import Path
import time
from pathlib import Path

BASE_DIR = Path("/opt/baal-agent/workspace/vox-director")
CLIENT_SECRET_FILE = BASE_DIR / "client_secret.json"
TOKEN_FILE = BASE_DIR / ".youtube_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"


def get_authenticated_service():
    """Get authenticated YouTube service using saved OAuth token."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    import httplib2

    if not TOKEN_FILE.exists():
        print("ERROR: No YouTube token found. Run: python3 youtube_direct.py --auth")
        sys.exit(1)

    token_data = json.loads(TOKEN_FILE.read_text())
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        print("  Refreshing YouTube token...")
        creds.refresh(Request())
        # Save refreshed token
        TOKEN_FILE.write_text(json.dumps(json.loads(creds.to_json()), indent=2))
        os.chmod(TOKEN_FILE, 0o600)

    if not creds.valid:
        print("ERROR: YouTube token invalid. Re-run: python3 youtube_direct.py --auth")
        sys.exit(1)

    return build(API_SERVICE_NAME, API_VERSION, credentials=creds)


def do_auth_flow():
    """Run the OAuth flow to get initial credentials.

    Works with both 'web' and 'installed' (Desktop app) credential types.
    For web type: uses manual copy-paste flow (headless server has no browser).
    For installed type: uses run_local_server (opens browser).
    """
    if not CLIENT_SECRET_FILE.exists():
        print("ERROR: client_secret.json not found!")
        print()
        print("Setup instructions:")
        print("  1. Go to https://console.cloud.google.com/")
        print("  2. Create a project (or select existing)")
        print("  3. Enable 'YouTube Data API v3':")
        print("     https://console.cloud.google.com/apis/library/youtube.googleapis.com")
        print("  4. Go to 'Credentials' → 'Create Credentials' → 'OAuth client ID'")
        print("  5. Application type: 'Web application'")
        print("  6. Add redirect URI: https://chimney-copper-marriage-salute.2n6.me/vox/api/yt/callback")
        print("  7. Download the JSON file")
        print(f"  8. Save it to: {CLIENT_SECRET_FILE}")
        print()
        print("  Then authorize via the Studio UI:")
        print("  https://chimney-copper-marriage-salute.2n6.me/vox/")
        print("  → Click 'Authorize YouTube' button")
        sys.exit(1)

    # Read the client config to determine type
    import json as _json
    client_config = _json.loads(CLIENT_SECRET_FILE.read_text())

    if "web" in client_config:
        # Web app type — use manual flow (headless server)
        print("=" * 60)
        print("  YouTube OAuth Authorization (Web App)")
        print("=" * 60)
        print()
        print("  This is a Web application credential type.")
        print("  Please authorize via the Studio UI instead:")
        print()
        print("  https://chimney-copper-marriage-salute.2n6.me/vox/")
        print("  → Click 'Authorize YouTube' button")
        print()
        print("  The Studio will handle the OAuth flow with the public redirect URI.")
        sys.exit(0)
    else:
        # Desktop app type — use local server (opens browser)
        from google_auth_oauthlib.flow import InstalledAppFlow
        print("Starting YouTube OAuth flow (Desktop app)...")
        print("  A browser window will open for authorization.")
        print()
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
        creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    # Save token
    token_data = json.loads(creds.to_json())
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    os.chmod(TOKEN_FILE, 0o600)
    print(f"\n✅ YouTube token saved to {TOKEN_FILE}")
    print("  You can now upload videos automatically!")


def set_thumbnail(youtube, video_id, thumbnail_path):
    """Set a custom thumbnail for an uploaded video.

    Requires the OAuth token to have the youtube scope (not just youtube.upload).
    """
    from googleapiclient.http import MediaFileUpload
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg")
        ).execute()
        print(f"  ✅ Thumbnail set!")
        return True
    except Exception as e:
        print(f"  ⚠️ Could not set thumbnail: {e}")
        return False


def upload_video(video_path, title, description, tags, privacy="public", thumbnail_path=None):
    """Upload video to YouTube."""
    from googleapiclient.http import MediaFileUpload

    youtube = get_authenticated_service()

    # Ensure video_path is a Path object
    video_path = Path(video_path)

    body = {
        "snippet": {
            "title": title[:100],  # YouTube max 100 chars
            "description": description or "",
            "tags": tags or [],
            "categoryId": "22",  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    # Use resumable upload for large videos
    chunk_size = 10 * 1024 * 1024  # 10MB chunks
    media = MediaFileUpload(str(video_path), chunksize=chunk_size, resumable=True)

    print(f"  Starting resumable upload ({video_path.stat().st_size / 1024 / 1024:.1f} MB)...")

    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    # Resumable upload with retry
    MAX_RETRIES = 10
    response = None
    retry = 0
    while response is None:
        try:
            status, response = insert_request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"  Upload progress: {pct}%")
            if response is not None:
                if "id" in response:
                    video_id = response["id"]
                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                    print(f"  ✅ Upload complete! Video ID: {video_id}")

                    # Set custom thumbnail if provided
                    if thumbnail_path and Path(thumbnail_path).exists():
                        set_thumbnail(youtube, video_id, thumbnail_path)

                    return {
                        "video_id": video_id,
                        "video_url": video_url,
                        "status": "done",
                    }
                else:
                    print(f"  ❌ Upload failed: {response}")
                    return {"error": str(response), "status": "error"}
        except Exception as e:
            retry += 1
            if retry > MAX_RETRIES:
                print(f"  ❌ Upload failed after {MAX_RETRIES} retries: {e}")
                return {"error": str(e), "status": "error"}
            sleep_time = min(2 ** retry, 60)
            print(f"  ⚠️  Retry {retry}/{MAX_RETRIES} after {sleep_time}s: {e}")
            time.sleep(sleep_time)

    return {"error": "Upload did not complete", "status": "error"}


def get_metadata(project_dir):
    """Extract title, description, tags from beats.json."""
    beats_path = Path(project_dir) / "beats.json"
    if not beats_path.exists():
        return None, None, []

    doc = json.loads(beats_path.read_text())
    topic = doc.get("topic", doc.get("title", "Untitled Video"))
    theme = doc.get("theme", "vox")

    # Use YouTube-optimized title if available
    yt_title = doc.get("yt_title", f"{topic} | {theme.title()} Documentary")

    desc_lines = []
    yt_desc = doc.get("yt_description", "")
    if yt_desc:
        desc_lines.append(yt_desc)
    else:
        desc_lines.append(f"{topic}")
        desc_lines.append("")
        desc_lines.append(f"Style: {theme} animation collage")
        desc_lines.append("")
        desc_lines.append("Generated by Vox Director Studio")

    # Add chapters
    beats = doc.get("beats", [])
    if beats:
        desc_lines.append("")
        desc_lines.append("Chapters:")
        chapter_time = 0
        for i, beat in enumerate(beats, 1):
            headline = beat.get("title_en", beat.get("title", beat.get("headline", f"Part {i}")))
            minutes = int(chapter_time // 60)
            seconds = int(chapter_time % 60)
            desc_lines.append(f"{minutes:02d}:{seconds:02d} {headline}")
            # Estimate time: each beat has 2 shots, each ~5s
            shots = beat.get("shots", [])
            chapter_time += sum(s.get("dur", 5) for s in shots) if shots else 10

    description = "\n".join(desc_lines)
    tags = doc.get("yt_tags", [theme, "animation", "short", "educational"])
    if topic:
        tags.extend(topic.lower().split()[:3])

    return yt_title, description, tags


def main():
    parser = argparse.ArgumentParser(description="Upload video to YouTube via Google Data API")
    parser.add_argument("project_dir", nargs="?", help="Path to project directory")
    parser.add_argument("--title", default=None)
    parser.add_argument("--description", default=None)
    parser.add_argument("--tags", default=None, help="Comma-separated tags")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    parser.add_argument("--video", default=None, help="Path to video file")
    parser.add_argument("--auth", action="store_true", help="Run OAuth authorization flow")
    args = parser.parse_args()

    if args.auth:
        do_auth_flow()
        return

    if not args.project_dir:
        print("ERROR: project_dir required (or use --auth for authorization)")
        sys.exit(1)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.exists():
        print(f"ERROR: Project directory not found: {project_dir}")
        sys.exit(1)

    # Find video file
    video_path = Path(args.video) if args.video else (project_dir / "final.mp4")
    if not video_path.exists():
        for name in ["final.mp4", "output.mp4", "video.mp4"]:
            candidate = project_dir / name
            if candidate.exists():
                video_path = candidate
                break
    if not video_path.exists():
        print(f"ERROR: Video file not found in {project_dir}")
        sys.exit(1)

    # Get metadata
    title, description, tags = get_metadata(project_dir)
    if args.title:
        title = args.title
    if args.description:
        description = args.description
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",")]

    print(f"=== YouTube Upload (Direct Google API) ===")
    print(f"  Project: {project_dir}")
    print(f"  Video: {video_path} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  Title: {title}")
    print()

    # Auto-generate thumbnail if not provided
    thumbnail_path = project_dir / "thumbnail.jpg"
    if not thumbnail_path.exists():
        # Try thumbnail_v1.jpg as fallback
        thumb_v1 = project_dir / "thumbnail_v1.jpg"
        if thumb_v1.exists():
            thumbnail_path = thumb_v1
        else:
            thumbnail_path = None
            print("  ⚠️ No thumbnail found, uploading without custom thumbnail")
    else:
        print(f"  Thumbnail: {thumbnail_path}")

    result = upload_video(video_path, title, description, tags, args.privacy, thumbnail_path=thumbnail_path)
    print()
    print(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
