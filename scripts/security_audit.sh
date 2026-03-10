#!/bin/bash
# Security audit script - run before every deployment

echo "=================================="
echo "AlphaLive Security Audit"
echo "=================================="
echo ""

ISSUES_FOUND=0

# CHECK 1: No API keys in git history
echo "[1] Checking git history for leaked credentials..."
if git log -p | grep -iE "(APCA-API|sk_|TELEGRAM.*TOKEN)" > /dev/null 2>&1; then
    echo "❌ CRITICAL: API keys found in git history!"
    echo "   Run: git filter-repo --path <file> --invert-paths"
    echo "   Or use BFG Repo Cleaner to remove secrets"
    ISSUES_FOUND=$((ISSUES_FOUND+1))
else
    echo "✓ No credentials in git history"
fi

# CHECK 2: No secrets in config files
echo ""
echo "[2] Checking config files for hardcoded secrets..."
if grep -rE "(APCA-API|sk_|[0-9]{10}:[A-Za-z0-9_-]{35})" configs/ 2>/dev/null; then
    echo "❌ CRITICAL: Hardcoded secrets found in configs/"
    echo "   Move all secrets to environment variables"
    ISSUES_FOUND=$((ISSUES_FOUND+1))
else
    echo "✓ No hardcoded secrets in configs/"
fi

# CHECK 3: .env file not committed
echo ""
echo "[3] Checking .env is gitignored..."
if [ -f ".env" ] && ! git check-ignore .env > /dev/null 2>&1; then
    echo "❌ WARNING: .env file is NOT in .gitignore"
    echo "   Add '.env' to .gitignore immediately"
    ISSUES_FOUND=$((ISSUES_FOUND+1))
else
    echo "✓ .env properly gitignored"
fi

# CHECK 4: HEALTH_SECRET set
echo ""
echo "[4] Checking HEALTH_SECRET is configured..."
if [ -z "$HEALTH_SECRET" ]; then
    echo "⚠️  WARNING: HEALTH_SECRET not set"
    echo "   Health endpoint will be disabled (503 responses)"
    echo "   This is OK for local dev, but required for Railway"
else
    if [ ${#HEALTH_SECRET} -lt 16 ]; then
        echo "❌ WARNING: HEALTH_SECRET is too short (<16 chars)"
        echo "   Generate a stronger secret: openssl rand -hex 32"
        ISSUES_FOUND=$((ISSUES_FOUND+1))
    else
        echo "✓ HEALTH_SECRET configured and strong"
    fi
fi

# CHECK 5: Telegram chat_id authentication
echo ""
echo "[5] Checking Telegram command authentication..."
if grep -n "msg_chat_id == self.chat_id" alphalive/notifications/telegram_commands.py > /dev/null 2>&1; then
    echo "✓ Telegram commands check chat_id"
else
    echo "❌ CRITICAL: Telegram commands don't check chat_id!"
    echo "   Anyone with bot token could send commands"
    ISSUES_FOUND=$((ISSUES_FOUND+1))
fi

# CHECK 6: Rate limiting on Telegram commands
echo ""
echo "[6] Checking Telegram command rate limiting..."
if grep -n "rate_limit" alphalive/notifications/telegram_commands.py > /dev/null 2>&1; then
    echo "✓ Telegram command rate limiting implemented"
else
    echo "⚠️  WARNING: No rate limiting on Telegram commands"
    echo "   Consider adding max 10 commands/minute to prevent abuse"
fi

# CHECK 7: No .env file in tracked files
echo ""
echo "[7] Checking .env is not tracked by git..."
if git ls-files | grep "^\.env$" > /dev/null 2>&1; then
    echo "❌ CRITICAL: .env file is tracked by git!"
    echo "   Remove it: git rm --cached .env"
    echo "   Then add to .gitignore"
    ISSUES_FOUND=$((ISSUES_FOUND+1))
else
    echo "✓ .env not tracked by git"
fi

echo ""
echo "=================================="
if [ $ISSUES_FOUND -eq 0 ]; then
    echo "✅ Security audit passed"
    echo "Safe to deploy to production"
    exit 0
else
    echo "❌ $ISSUES_FOUND security issues found"
    echo "Fix issues above before deploying to production"
    exit 1
fi
