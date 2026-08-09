#!/usr/bin/env python3
"""
YouTube Upload via Composio MCP Gateway
========================================
Uploads a generated video to YouTube using Composio's MCP server.

Authentication flow:
  1. JWT Bearer token (from OAuth flow, stored in .composio_token)
  2. x-consumer-api-key header (MCP consumer key, stored in .composio_key)

Usage:
  python3 youtube.py <project_dir> [--title "..."] [--description "..."] [--tags tag1,tag2]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path("/opt/baal-agent/workspace/vox-director")
CONSUMER_KEY_FILE = BASE_DIR / ".composio_key"
TOKEN_FILE = BASE_DIR / ".composio_token"
MCP_URL = "https://connect.composio.dev/mcp"


def load_keys():
    """Load consumer key and JWT token from files."""
    consumer_key = ""
    jwt_token = ""

    if CONSUMER_KEY_FILE.exists():
        consumer_key = CONSUMER_KEY_FILE.read_text().strip()

    if TOKEN_FILE.exists():
        jwt_token = TOKEN_FILE.read_text().strip()

    return consumer_key, jwt_token


def mcp_request(method, params=None, jwt_token="", consumer_key=""):
    """Send a JSON-RPC request to the Composio MCP server."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2024-11-05",
    }
    if jwt_token:
        headers["Authorization"] = f"Bearer {jwt_token}"
    if consumer_key:
        headers["x-consumer-api-key"] = consumer_key

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
    }
    if params:
        payload["params"] = params

    r = httpx.post(MCP_URL, headers=headers, json=payload, timeout=300)
    return r


def parse_mcp_response(response):
    """Parse MCP JSON-RPC response (handles both JSON and SSE formats)."""
    text = response.text
    # SSE format: lines starting with "data: "
    if "data: " in text:
        for line in text.split("\n"):
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
    # Plain JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": f"Could not parse response: {text[:200]}"}


def upload_to_youtube(video_path, title, description, tags, privacy="public", jwt_token="", consumer_key=""):
    """Upload video to YouTube via Composio MCP."""
    print(f"  Initializing MCP session...")
    # Step 1: Initialize
    r = mcp_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "vox-director", "version": "1.0"}
    }, jwt_token, consumer_key)

    if r.status_code != 200:
        print(f"  ❌ Initialize failed: HTTP {r.status_code}")
        print(f"  Response: {r.text[:300]}")
        return None

    init_resp = parse_mcp_response(r)
    print(f"  ✅ MCP initialized: {init_resp.get('result', {}).get('serverInfo', {}).get('name', 'unknown')}")

    # Get session ID from headers
    session_id = r.headers.get("mcp-session-id", "")
    if session_id:
        print(f"  Session ID: {session_id}")

    # Step 2: List tools to find YouTube upload tool
    print(f"  Listing available tools...")
    r2 = mcp_request("tools/list", {}, jwt_token, consumer_key)
    if r2.status_code == 200:
        tools_resp = parse_mcp_response(r2)
        tools = tools_resp.get("result", {}).get("tools", [])
        yt_tools = [t for t in tools if "youtube" in t.get("name", "").lower() or "upload" in t.get("name", "").lower()]
        print(f"  Found {len(tools)} tools ({len(yt_tools)} YouTube-related)")
        for t in yt_tools[:5]:
            print(f"    - {t['name']}")
    else:
        print(f"  ⚠️  tools/list returned HTTP {r2.status_code}")

    # Step 3: Execute YouTube upload tool
    # Find the upload tool name
    upload_tool = None
    if r2.status_code == 200:
        tools_resp = parse_mcp_response(r2)
        tools = tools_resp.get("result", {}).get("tools", [])
        for t in tools:
            name = t.get("name", "")
            if "youtube" in name.lower() and "upload" in name.lower():
                upload_tool = name
                break
            elif "upload" in name.lower() and "video" in name.lower():
                upload_tool = name
                break

    if not upload_tool:
        # Try common tool names
        upload_tool = "YOUTUBE_UPLOAD_VIDEO"

    print(f"  Using tool: {upload_tool}")
    print(f"  Uploading: {video_path}")

    # Read video file as base64 for MCP tool call
    import base64
    with open(video_path, "rb") as f:
        video_b64 = base64.b64encode(f.read()).decode()

    arguments = {
        "title": title,
        "description": description or "",
        "tags": tags or [],
        "privacy_status": privacy,
        "file": video_b64,
    }

    r3 = mcp_request("tools/call", {
        "name": upload_tool,
        "arguments": arguments,
    }, jwt_token, consumer_key)

    if r3.status_code != 200:
        print(f"  ❌ Upload failed: HTTP {r3.status_code}")
        print(f"  Response: {r3.text[:500]}")
        return None

    result = parse_mcp_response(r3)
    print(f"  Upload response: {json.dumps(result, indent=2)[:500]}")
    return result


def get_metadata(project_dir):
    """Extract title, description, tags from beats.json."""
    beats_path = Path(project_dir) / "beats.json"
    if not beats_path.exists():
        return None, None, []

    doc = json.loads(beats_path.read_text())
    topic = doc.get("topic", doc.get("title", "Untitled Video"))
    theme = doc.get("theme", "vox")

    title = f"{topic} | {theme.upper()} Animation"

    desc_lines = [
        f"{topic}",
        "",
        f"Style: {theme} animation collage",
        "",
        "Generated by Vox Director Studio",
    ]

    beats = doc.get("beats", [])
    if beats:
        desc_lines.append("")
        desc_lines.append("Chapters:")
        for i, beat in enumerate(beats, 1):
            headline = beat.get("headline", beat.get("title", f"Part {i}"))
            desc_lines.append(f"{i}. {headline}")

    description = "\n".join(desc_lines)
    tags = [theme, "animation", "short", "educational"]
    if topic:
        tags.extend(topic.lower().split()[:3])

    return title, description, tags


def main():
    parser = argparse.ArgumentParser(description="Upload video to YouTube via Composio MCP")
    parser.add_argument("project_dir", help="Path to project directory")
    parser.add_argument("--title", default=None)
    parser.add_argument("--description", default=None)
    parser.add_argument("--tags", default=None, help="Comma-separated tags")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    parser.add_argument("--video", default=None, help="Path to video file")
    args = parser.parse_args()

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

    print(f"=== YouTube Upload via Composio MCP ===")
    print(f"  Project: {project_dir}")
    print(f"  Video: {video_path} ({video_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  Title: {title}")
    print()

    # Load credentials
    consumer_key, jwt_token = load_keys()
    if not consumer_key:
        print("ERROR: No Composio consumer key found. Save it to .composio_key")
        sys.exit(1)
    if not jwt_token:
        print("ERROR: No JWT token found. Complete OAuth flow first.")
        print("  Open: https://chimney-copper-marriage-salute.2n6.me/vox/ → Connect Composio")
        sys.exit(1)

    print(f"  Consumer key: {consumer_key[:10]}...")
    print(f"  JWT token: {jwt_token[:20]}...")
    print()

    # Upload
    print("Uploading to YouTube...")
    try:
        result = upload_to_youtube(
            str(video_path), title, description, tags, args.privacy,
            jwt_token, consumer_key
        )
        print()
        if result and "error" not in result:
            print("=== Upload Result ===")
            print(json.dumps(result, indent=2, default=str)[:500])
            # Try to extract video URL
            content = result.get("result", {}).get("content", [])
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    text = item["text"]
                    if "watch?v=" in text or "youtu.be" in text:
                        print(f"\n✅ Video uploaded! Check response for URL.")
        else:
            print("❌ Upload failed")
            if result:
                print(json.dumps(result, indent=2)[:500])
    except Exception as e:
        print(f"\n❌ Upload failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
