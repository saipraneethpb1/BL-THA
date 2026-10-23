# Connecting a real Gmail account (Inbox Agent)

The Inbox Agent can work on a real Gmail inbox: new mail is read, labelled, drafted, and the
reply you approve (or that has earned auto-send) is sent from that Gmail account, in the same
thread. This needs a Google Cloud OAuth client. It takes about 10 minutes and costs nothing.

**Privacy.** "Connect Gmail" signs you in with Google and opens a private workspace that only
that Google login can see. The demo accounts (demo1-demo5) and the guest inbox are shared and
public, so Gmail never goes there. Tokens are stored encrypted; Disconnect revokes them.

## 1. Create a project

1. Open https://console.cloud.google.com/ and sign in with the Google account you'll use.
2. Top bar → project picker → **New project**. Name it e.g. `BL Inbox Agent` → **Create**,
   then make sure it is selected.

## 2. Turn on the Gmail API

**APIs & Services → Library**, search **Gmail API** → **Enable**.

## 3. OAuth consent screen (Google Auth Platform)

**APIs & Services → OAuth consent screen** (newer consoles: **Google Auth Platform**).

1. **Get started** / **Branding**: App name `BL Inbox Agent`, your email as support and
   developer contact.
2. **Audience**: **External**. Leave publishing status at **Testing**.
3. **Test users** → **Add users**: your own Gmail address, plus any reviewer who should be
   able to connect their own Gmail (up to 100). In Testing mode only listed users can sign in.
4. **Data access** (or Scopes) → **Add or remove scopes** → add
   `https://www.googleapis.com/auth/gmail.modify` (read, label, send; it cannot permanently
   delete mail). Save.
5. **Branding → Authorized domains**: add `sslip.io`.

Testing mode: Google shows an "unverified app" notice (click **Continue**), and a sign-in lasts
7 days before it must be connected again. That is fine for a demo.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID** (or **Clients → Create client**).

- Application type: **Web application**, name `Inbox Agent`.
- **Authorized redirect URIs** → add both:
  - `https://inbox.13-203-203-28.sslip.io/api/gmail/callback`
  - `http://localhost:8002/api/gmail/callback`
- **Create**. Keep the dialog open: it shows the **Client ID** and **Client secret**.

## 5. Give the keys to the app (you type them; they never go through chat)

**On the server** (from your own terminal):

```bash
ssh -i ~/.ssh/bl-tha-key.pem ubuntu@13.203.203.28
cd ~/app && bash deploy/set-gmail.sh
```

It asks for the client ID and secret (hidden input), saves them to `.env`, and restarts the
Inbox Agent.

**Locally** (optional), add two lines to the project's `.env`:

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

then `docker compose up -d inbox-agent`.

## 6. Use it

Open the Inbox Agent → **Connect Gmail** (left sidebar) → choose your Google account → allow.
You land in your private Gmail workspace. **Sync now** imports recent inbox mail (and it
re-syncs every few minutes while connected); each new email is labelled and drafted. Labels
also appear in Gmail under `Inbox Agent/...`. Approve a draft and it is sent from your Gmail as
a reply in the same thread.

Never paste the client secret into chat, issues or commits.
