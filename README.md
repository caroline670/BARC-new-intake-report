# BARC Dog Watch

A tiny robot that checks BARC's public adoption listing every morning,
figures out which dogs are new since yesterday, and emails you the list.

## How it works (the short version)

Every dog on [24petconnect.com/BARCadopt](https://24petconnect.com/BARCadopt)
has an ID like `A2094187`. Each day the script:

1. Visits the listing and reads every dog on it.
2. Compares those IDs against `seen_ids.json` — its "notebook" of dogs it's
   already told you about.
3. Emails you only the ones that aren't in the notebook yet.
4. Updates the notebook and saves it back to the repo, so tomorrow's check
   starts from today's list.

GitHub Actions is the alarm clock: it wakes the script up on a schedule,
even when your laptop is off and you're not in a chat with Claude.

## One-time setup (about 10 minutes)

### 1. Create the repo
Create a new GitHub repository (private is fine — this doesn't need to be
public). Upload all the files in this folder, keeping the folder structure
(the `.github/workflows/daily.yml` file has to stay at that exact path).

### 2. Get a Gmail "app password"
This lets the script send email as you, without your real Gmail password.

1. Turn on 2-Step Verification on your Google account, if it isn't already
   (Google Account → Security → 2-Step Verification).
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it something like "BARC Dog Watch").
4. Copy the 16-character password it gives you — you won't see it again.

### 3. Add three secrets to the repo
In your new repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add:

| Secret name | Value |
|---|---|
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the 16-character app password from step 2 |
| `RECIPIENT_EMAIL` | where you want the digest sent (can be the same Gmail address) |

### 4. Test it
Go to the **Actions** tab → **BARC Dog Watch** → **Run workflow**. This
triggers it manually so you don't have to wait until tomorrow morning to
see if it works. Check your inbox.

### 5. Let it run
Once the test email looks right, you're done — it runs automatically every
day at roughly 8am Central.

## Two things worth knowing

- **The very first real run may look like a flood.** I seeded the notebook
  with the dogs I could see on the day I built this, but the shelter's
  listing has more dogs than fit on one page. The first automated run may
  therefore report a batch of dogs that were already there, not truly new.
  Every run after that will be accurate.
- **If the email ever says "nothing parsed today"** instead of a dog list,
  it means BARC changed how their page is laid out and the script's parser
  needs a small update — send me the email and I'll fix it.

## Running it yourself, locally, anytime

```
pip install -r requirements.txt
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="your16charpassword"
export RECIPIENT_EMAIL="you@gmail.com"
python scraper.py
```
