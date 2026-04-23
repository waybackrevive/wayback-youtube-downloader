#!/bin/bash
# Render.com build script
# Installs ffmpeg (needed by yt-dlp to merge video+audio) then Python deps

apt-get update && apt-get install -y --no-install-recommends ffmpeg
pip install -r requirements.txt
