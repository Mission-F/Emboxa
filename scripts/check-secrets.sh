#!/bin/sh
set -eu

if find . -path './.git' -prune -o -path './.venv' -prune -o -type f \( -name '.env' -o -name '*.db' -o -name '*.sqlite*' -o -name '*.eml' -o -name '*.mailvault' -o -name '*.key' \) -print | grep -q .; then
  echo "Refusing release: private data-like files are present."
  exit 1
fi

if grep -RIE --exclude-dir=.git --exclude-dir=.venv --exclude-dir=tests --exclude='check-secrets.sh' '(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|[0-9]{8,}:[A-Za-z0-9_-]{30,})' .; then
  echo "Review the possible credential above."
  exit 1
fi

echo "No obvious private data or hard-coded secrets found."
