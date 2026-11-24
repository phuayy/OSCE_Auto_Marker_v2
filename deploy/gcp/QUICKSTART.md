# Set up the OSCE Marker in your own Google Cloud account

This guide is for anyone, technical or not. You copy one command into a
browser window and answer a few questions. The script does the rest. Expect
**about 45 minutes**, most of it waiting.

What you end up with:

- **A start page**, which is the link you bookmark and share. Anyone you
  allow signs in with Google and presses **Start the marker**.
- **The marker itself**, on a GPU computer in Google Cloud. It switches
  itself off after 30 minutes of nobody using it, so you only pay for GPU time
  while people are actually marking.

## Before you start

You need:

1. **A Google account.** A personal Gmail or a work account both work.
2. **A Google Cloud billing account with a card on it.**
   - Go to <https://console.cloud.google.com/billing> and create one if you
     have none.
   - **A free trial cannot use GPUs.** If Google offers to *Activate full
     account* or *Upgrade*, accept. You keep any free credit.
3. **Optional: a HuggingFace account.** It gives the transcripts
   "Student"/"Examiner" speaker labels. You can skip it now and add it later.

## Step 1: Open Google Cloud Shell

Click this link. It opens a terminal in your browser with a copy of the app
already downloaded, so there is nothing to install:

**[Open in Google Cloud Shell](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https://github.com/phuayy/OSCE_Auto_Marker_v2&cloudshell_git_branch=main&cloudshell_tutorial=deploy/gcp/QUICKSTART.md&show=terminal)**

If Google asks whether to trust the repository, tick the box and press
**Confirm**. If it asks to authorise Cloud Shell, press **Authorise**.

## Step 2: Run the setup

Copy this line into the terminal at the bottom of the screen and press Enter:

```bash
bash deploy/gcp/setup.sh
```

Then answer the questions. Pressing Enter accepts the suggested answer in
`[brackets]`:

| Question | What to answer |
|---|---|
| **Project ID** | Type a new name such as `osce-marker-yourschool`. The script creates the project and links your billing account. To use an existing project, type its ID. |
| **Link billing account?** | Press Enter (yes). |
| **Region** | Pick one near your users, for example `asia-southeast1` (Singapore), `europe-west4` (Netherlands) or `us-central1` (Iowa). |
| **Allowed Google emails** | The Google accounts allowed to switch the marker on, separated by commas. Your own is suggested. |
| **Allowed email domains** | For example `school.edu` lets everyone with a school address switch it on. Leave it blank if you don't need this. |
| **HuggingFace token** | Optional. Follow the three steps the script prints, or press Enter to skip. |

## Step 3: Do the steps the script cannot do for you

The script stops and shows a box titled **ACTION NEEDED** when only you can do
the next step. There are three of these. Usually you see only the third.

### A. GPU quota (new projects only)

New Google Cloud projects are allowed **zero** GPUs until you ask for some.
If the script says so:

1. Open the link it prints (the *Quotas* page).
2. In the filter box, search for **GPUs (all regions)**. Tick it, press
   **Edit quotas**, enter `1` and submit. Give a short reason, for example
   *"One GPU machine for an exam video-marking app."*
3. Do the same for **NVIDIA L4 GPUs** in your region. Optionally do it for
   **NVIDIA T4 GPUs** in your region too, as a backup.
4. Wait for Google's approval email. This takes minutes to a day. Then open
   Cloud Shell again (Step 1) and run the same command. The script continues
   where it stopped.

### B. Your own domain (only if you chose one)

By default the marker gets a free address like
`https://34-87-1-2.sslip.io`, which needs no setup. If you passed
`--domain marker.yourschool.edu`, ask whoever manages that domain to create
the DNS record the script prints.

### C. Google sign-in for the start page (everyone, once)

Google has no command that creates this, so the script prints the exact
clicks and the exact address to paste. It takes about 3 minutes:

1. Open the first link, press **Get started**, name it `OSCE Marker`, pick
   your email, and choose an audience:
   - **Internal** if everyone signs in with your organisation's Google
     accounts.
   - **External** otherwise. If you choose External, also open the second
     link and press **Publish app**. No review is needed, because the page
     only asks for name and email.
2. Open the third link and create a client with type **Web application**.
   Under **Authorized JavaScript origins**, add the address the script shows
   (it starts with `https://osce-gate-`). Press **Create**.
3. Copy the **Client ID** and paste it into the terminal.

## Step 4: Wait, then sign in

The script now installs everything. The first time takes 15 to 30 minutes.
You can close the window: the install continues on its own, and running the
command again picks up where it left off.

When it finishes, the script prints:

- **The start page.** Bookmark it and share it with your markers.
- **The marker's address and the first admin password.** Sign in as `admin`
  with that password.

Inside the marker, as admin:

1. **Settings → Provider API keys:** add the key for the AI model that does
   the scoring (for example an NVIDIA key from <https://build.nvidia.com>).
2. **Settings → Users:** invite your markers. Each invitation shows a link
   for you to send them.
3. **Account:** change the admin password.

If you lose the admin password before changing it, open Cloud Shell and run:

```bash
gcloud secrets versions access latest --secret osce-admin-password
```

## Everyday use

- People open the **start page**. If the marker is off, they press
  **Start the marker** and wait a few minutes.
- The marker **switches itself off** after 30 minutes with nobody using it
  and no video being processed. Nothing is lost when it stops.

## What it costs (approximate, check Google's pricing for your region)

| | Cost |
|---|---|
| Marker running (L4 GPU) | about US$1 per hour |
| Marker running (T4 backup GPU) | about US$0.75 per hour |
| Marker switched off: disk (200 GB) plus fixed address | about US$25-30 per month |
| Start page | usually free (within Cloud Run's free tier) |

Set a budget alert at <https://console.cloud.google.com/billing/budgets> so
Google emails you before costs go past what you expect.

## Updating to a newer version

Open Cloud Shell (Step 1). The link downloads the newest version. Then run:

```bash
bash deploy/gcp/setup.sh --update
```

The marker picks up the update the next time it starts, or straight away if
you agree to restart it.

## Moving to another computer

Nothing is stored on your own computer. The settings are saved in your Google
Cloud project. From any computer, sign in to Google Cloud, open Cloud Shell
(Step 1), and run the setup again. It finds everything that already exists.

## Removing everything

To stop all charges, run:

```bash
bash deploy/gcp/teardown.sh
```

You type the project ID to confirm. **This deletes every video, score and
account on the marker.** You have two other options:

- `bash deploy/gcp/teardown.sh --keep-disk` removes everything except the
  marker's disk, so the data is kept. You pay about US$20 a month for the
  disk alone.
- `bash deploy/gcp/teardown.sh --delete-project` shuts down the whole Google
  Cloud project. You can undo this within 30 days.

## If something goes wrong

- **Run the same command again.** Steps that already finished are skipped.
  Most problems, such as a GPU being briefly unavailable or a slow approval,
  clear up on a second run.
- **"None of the GPUs is free right now"** means Google has no spare GPU in
  that zone at the moment. Wait 15 to 60 minutes and try again.
- **The install failed on the VM.** The script prints a command that shows
  the VM's install log. Send that output to whoever supports you.

Technical details are in [README.md](README.md).
