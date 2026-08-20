#!/bin/bash
set -e
cd /home/cltx/apps/ai-token-dashboard/current
git pull origin master
grep -q '^USAGE_TEAM_RENAME_MAP=' .env || echo 'USAGE_TEAM_RENAME_MAP=team-89a8a9e42a3b64fe=team-2ed30984b00bb482' >> .env
docker compose up -d --build
sleep 8
curl -fsS http://127.0.0.1:8000/api/health
echo DEPLOY_OK
