# Hosting Guide: Run Polymarket Monitor 24/7 on GitHub Actions (FREE)

This guide explains how to host `russia_ukraine_ceasefire.py` on GitHub Actions so it runs automatically every hour, even when your laptop is off.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1: Set Up Twilio for WhatsApp](#step-1-set-up-twilio-for-whatsapp)
3. [Step 2: Create a GitHub Repository](#step-2-create-a-github-repository)
4. [Step 3: Add Secret Credentials](#step-3-add-secret-credentials)
5. [Step 4: Push Your Code](#step-4-push-your-code)
6. [Step 5: Verify It's Working](#step-5-verify-its-working)
7. [Customization](#customization)
8. [Troubleshooting](#troubleshooting)
9. [Alternative Hosting Options](#alternative-hosting-options)

---

## Prerequisites

- A GitHub account (free)
- A Twilio account (free tier available)
- Git installed on your computer
- Your WhatsApp number verified with Twilio

---

## Step 1: Set Up Twilio for WhatsApp

Twilio is the most reliable way to send WhatsApp messages programmatically.

### 1.1 Create a Twilio Account

1. Go to [https://www.twilio.com/try-twilio](https://www.twilio.com/try-twilio)
2. Sign up for a free account
3. Verify your email and phone number

### 1.2 Get Your Twilio Credentials

1. Log into the [Twilio Console](https://console.twilio.com/)
2. On the dashboard, find and copy:
   - **Account SID** (starts with `AC...`)
   - **Auth Token** (click to reveal, starts with random characters)

### 1.3 Set Up WhatsApp Sandbox

For testing/personal use, Twilio's WhatsApp Sandbox is free:

1. In Twilio Console, go to **Messaging** → **Try it out** → **Send a WhatsApp message**
2. You'll see a sandbox number like `+14155238886`
3. **Important**: Send the join code from YOUR WhatsApp to this number
   - Open WhatsApp on your phone
   - Send a message like `join <your-code>` to `+14155238886`
   - You'll receive a confirmation message

### 1.4 Note Your WhatsApp Number Format

Your WhatsApp number must be in E.164 format:
- `+919677131225` (India country code +91)
- The script is pre-configured with `whatsapp:+919677131225`

---

## Step 2: Create a GitHub Repository

### 2.1 Create a New Repository on GitHub

1. Go to [https://github.com/new](https://github.com/new)
2. Repository name: `polymarket-ceasefire-monitor` (or any name you like)
3. Set to **Private** (recommended, to keep your monitoring private)
4. Click **Create repository**

### 2.2 Initialize Local Git Repository

Open a terminal in your project folder:

```bash
cd /home/saksham/projects/polymarket

# Initialize git
git init

# Add all files
git add .

# Create initial commit
git commit -m "Initial commit: Polymarket ceasefire monitor"

# Add GitHub as remote (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/polymarket-ceasefire-monitor.git

# Push to GitHub
git branch -M main
git push -u origin main
```

---

## Step 3: Add Secret Credentials

**Never commit credentials to your repository!** Use GitHub Secrets instead.

### 3.1 Navigate to Repository Secrets

1. Go to your repository on GitHub
2. Click **Settings** (tab at the top)
3. In the left sidebar, click **Secrets and variables** → **Actions**
4. Click **New repository secret**

### 3.2 Add the Following Secrets

Add these three secrets one by one:

| Secret Name | Value |
|-------------|-------|
| `TWILIO_ACCOUNT_SID` | Your Account SID (e.g., `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`) |
| `TWILIO_AUTH_TOKEN` | Your Auth Token |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` (Twilio sandbox number) |

For each secret:
1. Click **New repository secret**
2. Enter the **Name** exactly as shown above
3. Enter the **Secret** value
4. Click **Add secret**

---

## Step 4: Push Your Code

If you haven't already pushed, or if you made changes:

```bash
git add .
git commit -m "Add GitHub Actions workflow"
git push origin main
```

Your repository should now contain:
```
polymarket-ceasefire-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml
├── russia_ukraine_ceasefire.py
├── requirements.txt
├── HOSTING_GUIDE.md
└── README.md (optional)
```

---

## Step 5: Verify It's Working

### 5.1 Manually Trigger the Workflow

1. Go to your repository on GitHub
2. Click the **Actions** tab
3. Click on **Monitor Polymarket Ceasefire Odds** in the left sidebar
4. Click the **Run workflow** button (dropdown on right)
5. Click the green **Run workflow** button

### 5.2 Monitor the Run

1. Click on the running workflow to see details
2. Click on the **check-odds** job
3. Expand the **Run monitoring script** step to see output

You should see something like:
```
============================================================
Polymarket Russia-Ukraine Ceasefire Monitor
============================================================

Searching for market: Russia x Ukraine ceasefire by January 31, 2026?
Found market: Russia x Ukraine ceasefire by January 31, 2026?
Current 'Yes' odds: 15.50%
Threshold: 20.00%
No alert needed. Yes odds (15.50%) below threshold (20.00%).
```

### 5.3 Scheduled Runs

Once set up, the workflow will automatically run:
- **Every hour** at minute 0 (e.g., 1:00, 2:00, 3:00...)
- You can see past runs in the **Actions** tab

---

## Customization

### Change the Check Frequency

Edit `.github/workflows/monitor.yml` and modify the cron schedule:

```yaml
schedule:
  # Every 30 minutes
  - cron: '*/30 * * * *'
  
  # Every 6 hours
  - cron: '0 */6 * * *'
  
  # Once daily at midnight UTC
  - cron: '0 0 * * *'
  
  # Every 15 minutes (be careful with rate limits)
  - cron: '*/15 * * * *'
```

### Change the Odds Threshold

Edit `russia_ukraine_ceasefire.py`:

```python
ODDS_THRESHOLD = 0.25  # Change to 25%
```

### Change the Target WhatsApp Number

Edit `russia_ukraine_ceasefire.py`:

```python
TARGET_WHATSAPP_NUMBER = "whatsapp:+1XXXXXXXXXX"  # Your number
```

---

## Troubleshooting

### "Market not found" Error

The market title must match exactly. If the market is closed or renamed:
1. Go to [Polymarket](https://polymarket.com)
2. Find the exact market title
3. Update `MARKET_TITLE` in the script

### WhatsApp Message Not Received

1. **Verify sandbox enrollment**: Send the join message again to Twilio's sandbox number
2. **Check Twilio logs**: Go to Twilio Console → Monitor → Logs → Messages
3. **Check phone number format**: Must be `whatsapp:+[country_code][number]`
4. **Sandbox expires**: Twilio sandbox requires re-joining every 72 hours of inactivity

### GitHub Actions Not Running

1. **Check Actions tab**: Look for any error indicators
2. **Verify secrets**: Ensure all secrets are correctly named and set
3. **Check workflow file**: Ensure `.github/workflows/monitor.yml` is valid YAML

### Rate Limiting

If checking too frequently:
- GitHub Actions has limits (~2000 minutes/month on free tier)
- Polymarket API may rate-limit frequent requests
- Twilio has sending limits on free tier

---

## Alternative Hosting Options

### Option A: Railway (Free Tier)

[Railway](https://railway.app) offers simple deployment:

1. Connect your GitHub repo
2. Set environment variables in Railway dashboard
3. Railway auto-deploys on git push

### Option B: Render (Free Tier)

[Render](https://render.com) supports cron jobs:

1. Create a new **Cron Job** service
2. Connect to GitHub
3. Set schedule and environment variables

### Option C: AWS Lambda + EventBridge (Free Tier)

More complex but highly reliable:

1. Package script as Lambda function
2. Create EventBridge rule for scheduling
3. Set environment variables in Lambda config

### Option D: Google Cloud Functions + Cloud Scheduler

Similar to AWS:

1. Deploy as Cloud Function
2. Set up Cloud Scheduler trigger
3. Configure via GCP Console

### Option E: Your Own VPS (DigitalOcean, Linode, etc.)

For full control (~$5/month):

```bash
# On your server
git clone https://github.com/YOUR_USERNAME/polymarket-ceasefire-monitor.git
cd polymarket-ceasefire-monitor
pip install -r requirements.txt

# Set up cron job
crontab -e
# Add: 0 * * * * cd /path/to/repo && /usr/bin/python3 russia_ukraine_ceasefire.py >> /var/log/polymarket.log 2>&1
```

---

## Cost Summary

| Service | Cost |
|---------|------|
| GitHub Actions | Free (2000 min/month) |
| Twilio WhatsApp Sandbox | Free (for testing) |
| Twilio WhatsApp Business | ~$0.005/message |

**Total: $0** for personal/testing use with sandbox

---

## Security Notes

1. **Never commit credentials** - Always use GitHub Secrets or environment variables
2. **Use private repositories** - Keep your monitoring strategy private
3. **Rotate credentials** - Periodically update your Twilio tokens
4. **Monitor usage** - Check Twilio dashboard for unexpected activity

---

## Questions?

If you encounter issues:
1. Check the GitHub Actions logs
2. Check Twilio Console logs
3. Test the script locally first with credentials exported

```bash
export TWILIO_ACCOUNT_SID="your_sid"
export TWILIO_AUTH_TOKEN="your_token"
python russia_ukraine_ceasefire.py
```

Good luck with your monitoring! 🎯

