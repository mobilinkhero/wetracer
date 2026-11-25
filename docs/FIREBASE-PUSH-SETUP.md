# Firebase push setup (server / dashboard side)

How to turn on real push notifications so agent replies reach the visitor's
phone app, even when the app is closed.

## What Firebase push does here

When the AI or a human replies in a conversation, ChatterMate checks whether
the visitor has the app installed and is away:

- Visitor has the app open on that chat → no push. The reply already arrived
  live; a push would notify twice.
- Visitor is away and the app registered a device → one push notification per
  device, sent through Firebase Cloud Messaging (FCM).

Each organisation holds its own Firebase service-account key, so pushes go
out from your own Firebase project. The chat never breaks if push fails —
a failed push is logged and the conversation carries on.

## 1. Create a Firebase project (free)

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
   and sign in with a Google account.
2. Click **Add project** and follow the steps. The free Spark plan is enough
   for FCM.
3. That is all the console work the server side needs. (The app itself needs
   its own Firebase setup — `google-services.json` / `GoogleService-Info.plist`
   in the app — see the Flutter SDK README. This doc is only about the server
   key.)

## 2. Generate the service-account key

1. In the Firebase console, open your project, then **Project settings**
   (gear icon, top left).
2. Go to the **Service accounts** tab.
3. Click **Generate new private key**, then **Generate key**.
4. A JSON file downloads. It looks like this (yours will have real values):

```json
{
  "type": "service_account",
  "project_id": "your-project-id",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "firebase-adminsdk-xxxxx@your-project-id.iam.gserviceaccount.com",
  "client_id": "...",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token"
}
```

## 3. Paste the key in the dashboard

1. In ChatterMate, go to **Settings → API Docs**.
2. Find the **Push notifications (FCM)** card.
3. Paste the whole JSON file into the **Firebase service account** box.
4. Click **Save FCM key**.

The dashboard checks the key before saving: it must be a JSON object with
`"type": "service_account"`, a `project_id`, and a `private_key`. If it is
not, you get a plain error and nothing is saved.

## 4. What the status shows

After saving, the card shows a **Live** badge and:

- the Firebase project id the key belongs to,
- when the key was saved.

Before a key is added it shows **Test mode** — see below.

To **replace** the key (for example, you made a new Firebase project or
rotated the key), paste the new JSON in the same box and click
**Replace FCM key**. The old key is overwritten. There is no separate delete
button — replacing is how you change it.

## 5. Test mode — what happens until the key is added

With no key, nothing is sent and nothing breaks:

- The dashboard shows **Test mode** and the note "Pushes are recorded,
  not sent."
- Each would-be push is recorded with status `recorded_no_credentials`,
  so you can check what would have gone out at
  `GET /api/v1/mobile/push-attempts`.

As soon as a valid key is saved, real pushes start going out — no restart
and no reinstall needed.

## 6. Security — treat the key as a secret

The service-account JSON is a secret. It can send pushes (and more) as your
Firebase project, so:

- It is **stored encrypted** on the server, per organisation. The dashboard
  never shows it back — after saving, the box is empty and the status shows
  only the project id and the save time.
- **Never paste it in chat, email, or a ticket**, and never put it in the
  app. The app only ever holds its own device token; the service-account
  key lives server-side only.
- If a key ever leaks, go to the Firebase console → Service accounts and
  delete that key, generate a new one, and paste the new one in the
  dashboard as in step 3.

## API (if you prefer it to the dashboard)

- `POST /api/v1/admin/mobile/fcm-config` with
  `{"service_account_json": { ... }}` — stores the key. Returns
  `{"configured": true, "project_id": "..."}`. Admin only.
- `GET /api/v1/admin/mobile/fcm-config` — returns
  `{"configured": true/false, "project_id": "...", "updated_at": "..."}`.
  It never returns the key.
