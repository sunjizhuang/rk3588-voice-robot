#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
echo "完成。激活环境：source venv/bin/activate"

