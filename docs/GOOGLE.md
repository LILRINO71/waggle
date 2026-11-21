# Google for Waggle (Docs, Drive, Calendar, Tasks, Classroom)

Waggle uses a small free tool, gogcli (`gog`, MIT licence, version 0.48.1, pinned), to read and write your Google
account. It is already unpacked on this PC at `C:\Users\hiheo\.waggle-tools\gog\v0.48.1\gog.exe` (checked against the
SHA-256 in the release's `checksums.txt`). Until you do the one-time sign-in below, it does nothing, and
`waggle doctor` says `Google: not signed in (see docs/GOOGLE.md)`.

## What you need to do once (about 10 minutes, no payment, no card)

gog does **not** ship its own Google key. Google wants every program that signs in to a Google account to have a key
that belongs to somebody, so you make a free one in your own Google account. You are the only user.

Use your personal Gmail account for every step, in a browser that is signed in to it.

1. Go to https://console.cloud.google.com/projectcreate . If Google asks you to agree to terms, tick the box and click
   **Agree and continue**. Project name: type `Waggle`. Leave Organization/Location alone. Click **Create**. Wait for
   the bell icon at the top right to say the project is ready, then make sure `Waggle` is shown in the project picker
   at the top left (click it and choose `Waggle` if not).
2. Turn on five services. Open each link, check `Waggle` is the project at the top, and click the blue **Enable**
   button (if it says **Manage**, it is already on):
   - https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
   - https://console.cloud.google.com/apis/library/tasks.googleapis.com
   - https://console.cloud.google.com/apis/library/drive.googleapis.com
   - https://console.cloud.google.com/apis/library/docs.googleapis.com
   - https://console.cloud.google.com/apis/library/classroom.googleapis.com
3. Go to https://console.cloud.google.com/auth/overview and click **Get started**.
   - App name: `Waggle`. User support email: pick your Gmail. Click **Next**.
   - Audience: choose **External**. Click **Next**.
   - Contact information: type your Gmail. Click **Next**.
   - Tick **I agree to the Google API Services: User Data Policy**. Click **Continue**, then **Create**.
4. Make the key. Go to https://console.cloud.google.com/auth/clients and click **Create client**.
   - Application type: **Desktop app**. Name: `Waggle`. Click **Create**.
   - In the box that appears, click **Download JSON**. It saves a file called `client_secret_....json` into your
     Downloads folder. Click **OK**.
5. Stop Google from signing Waggle out every week. Go to https://console.cloud.google.com/auth/audience , click
   **Publish app**, then **Confirm**. (This does not send anything to Google for review. If you skip it, the sign-in
   dies after 7 days and you must repeat step 6.)
6. Sign in. Double-click **`Waggle Google Login.bat`** in the Waggle folder.
   - It finds the downloaded file, stores it, and asks for your Gmail address. Type it and press Enter.
   - Your browser opens. Pick your account.
   - A page says **Google hasn't verified this app**. That is expected (it is your own key). Click **Advanced**, then
     **Go to Waggle (unsafe)**.
   - A list of permissions appears. Click **Select all** (every box must be ticked or some features will not work),
     then **Continue**.
   - The browser says you can close the tab. The black window then prints `Google: signed in as ...`.
7. Optional: delete `client_secret_....json` from Downloads. Waggle already has what it needs.

Check it any time with `python -m waggle.google status`, or run `waggle doctor` (the **Google** line should say
`signed in as ...`).

If the Classroom line stays empty: open https://classroom.google.com signed in as this same Gmail. If your classes are
not there, they belong to a school account, which a personal sign-in cannot see (and schools often block this kind of
sign-in). Docs, Drive, Calendar and Tasks work anyway.

## Where the secrets live

- The sign-in token: Windows Credential Manager (gog's keyring). Never a file, never the repo, never printed.
- The key you downloaded: its secret half goes into Credential Manager too; only the public id is a file, under
  `C:\Users\hiheo\.waggle-tools\gog\home\` (outside AppData and outside the repo).
- To undo everything: switch Google off with `"google_gog": false` in `C:\Users\hiheo\.waggle\config.json`, remove the
  access at https://myaccount.google.com/permissions (choose `Waggle`, **Delete all connections**), and delete the
  `Waggle` project at https://console.cloud.google.com/cloud-resource-manager .

## What Waggle can and cannot do with it

Exact list, enforced by gog itself on every call (`--enable-commands-exact` with only that one command, `--gmail-no-send`,
`--wrap-untrusted`, `--no-input`):

| Service | Allowed | Never |
|---|---|---|
| Docs | create, read, add text to the end | delete, replace, share |
| Drive | search, read file info | delete, upload, move, share, change permissions |
| Calendar | list events, create an event on your own calendar | invite anyone, delete, edit |
| Tasks | list, add, mark done | delete, clear |
| Classroom | list your courses, assignments, due dates, whether you turned one in | turn in, grade, post, join or leave |
| Gmail | not signed in to it at all (the sign-in does not ask for Gmail) | anything |

Whatever Google returns (a doc's text, an event title, an assignment name) reaches Claude marked as untrusted data, so
words inside it can never give Waggle orders.

In chat, the read tools are free to use. The five tools that make something (`google_doc_new`, `google_doc_append`,
`google_event_new`, `google_task_add`, `google_task_done`) go through the same owner-instruction rule as other
actions: they work only in a message you typed or said yourself, never from a web page, email, file or background job.

Without Claude (`python -m waggle.google ...`): `status`, `docs-new TITLE`, `tasks`, `classroom-due`. The morning brief
gets one extra line, `Google due soon: ... | tasks: ...`, only when you are signed in and there is something to say.

## Switch

`google_gog` in `C:\Users\hiheo\.waggle\config.json` (default `true`). `false` turns every Google tool and brief line off.
Optional `google_account` picks one account if you ever sign in more than one.

## Updating gog

The version is pinned in `waggle/google.py` (`GOG_VERSION`, `GOG_SHA256`). To move to a newer release, read its
release notes, copy the new Windows zip's SHA-256 from the release's `checksums.txt`, change both lines, and run
`python -m waggle.google install`.
