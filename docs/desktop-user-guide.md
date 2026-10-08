# OSCE AI Marker — User Guide

**For examiners and staff who use the marker. No technical knowledge needed.**

The program is already installed on this computer. You open it with an icon
on the Desktop and use it in your web browser. It does not need an internet
website address: it runs on this computer only.

If anything in this guide doesn't match what you see, or a step says
"contact your technical contact", that person is: _______________________
(phone / email: _______________________).

---

## 1. What is on your Desktop

| Icon | What it does |
|---|---|
| **OSCE AI Marker** | Starts the program and opens it in your browser |
| **Stop OSCE AI Marker** | Shuts the program down when you have finished for the day |

---

## 2. Starting the program

1. Double-click **OSCE AI Marker** on the Desktop.
2. A blue window appears and shows its progress. **Wait.** It usually takes
   about 1 minute, and can take a few minutes after the computer has just
   been switched on.
3. Your browser opens the OSCE AI Marker sign-in page by itself.
4. The blue window closes by itself.

**Two small windows are now minimised on the taskbar:**
- **OSCE AI Marker - WEBSITE**
- **OSCE AI Marker - WORKER**

**Leave them open.** They *are* the program. Closing either one stops
marking.

> If the browser says "can't connect" or "this site can't be reached", wait
> 30 seconds and press **F5** to refresh.
>
> If the blue window turns red and says it could not start, **take a photo or
> screenshot of it** and send it to your technical contact.

To come back later without restarting, open your browser and go to
**http://localhost:8787**. Bookmark it.

---

## 3. Signing in

1. Type your **username or email** and your **password**, then click
   **Sign in**.
2. **Forgot your password?** Click **"Forgot your password?"**, or ask an
   administrator to reset it from the **Users** page.

**The very first time**, you sign in with the administrator details your
technical contact gave you. Then go straight to **Account** (top right) and
**change the password** to one only you know.

---

## 4. First-time setup (administrator, once)

Do this once, ideally with your technical contact beside you. Everything is
in the browser.

### 4.1 Add an AI key
The marking is done by an online AI service. It needs a key, which works like
a password for that service.
1. Click **Settings**.
2. Find the **Provider API keys** card.
3. Next to the service you have a key for (for example NVIDIA or OpenAI),
   paste the key.
4. Click **Test**. It should say the test succeeded.
5. Click **Save**.

Two keys from two different services are better. If one service is down,
the other takes over automatically.

### 4.2 Choose the AI model
1. In **Settings**, find the **Scoring model** card.
2. Choose the **primary** model and a **fallback** model. Your technical
   contact can recommend which.
3. Save.

### 4.3 Check transcription
In **Settings → Transcription engine**, **WhisperX** should be selected and
shown as available. Leave it as it is.

### 4.4 Upload the communication rubric
1. Click **Communication Rubric** at the top.
2. Upload the rubric PDF (for example *PHR1012 OSCE Rubric.pdf*).
3. Check that the list of criteria it shows looks right.

### 4.5 Add your colleagues
1. Click **Users**.
2. Under **Invite a marker**, enter their email address and name, and choose
   a role:
   - **Marker** can mark and view everything.
   - **Admin** can also manage users and settings.
3. Click to invite. The screen shows an **invitation link**.
4. **Copy the link and email it to them yourself.** The program doesn't send
   emails. The link works for 3 days; if it runs out, click
   **Send a new invitation**.
5. When they open the link, they choose their own password.

---

## 5. Marking one student

You need:
- the **video** of the station (for example `.mp4`);
- the station's **case study PDF**, which contains the marking checklist.

Steps:
1. On the main page, find **Upload a station recording**.
2. Choose **One student**.
3. Choose the **video file** and the **case study** PDF.
4. *(Optional)* Give the session a name, for example *"Tinea – Station 3 – J.
   Tan"*.
5. *(Optional)* Choose a list of medical terms. This helps drug and condition
   names come out spelled correctly.
6. Click **Upload and start assessment** and confirm.
7. A box shows the upload. **Keep the browser open until the upload
   finishes.** After that you can close the box and do other things.

The session appears in **Saved sessions** with a progress bar. It goes
through: *Queued → Transcribing → Scoring → Completed*.

**How long?** A 10-minute video usually takes about 5–15 minutes. The very
first video after installation is slower, because the program downloads its
speech model once. You can start several videos; they wait their turn.

When the session shows **Completed**, click **Open**.

---

## 6. Marking a recording with several students

Use this when one long video contains several students one after another.

1. In **Upload a station recording**, choose **Several students, one
   recording**.
2. Choose how the program should find where each student starts and ends:
   - **Bell detection** — uses the station bell. Best when a bell rings
     between students.
   - **Human detection** — watches who is on screen. Choose the rule that
     matches how the camera was set up; **People on screen** = 2 is the usual
     choice.
3. Choose the video and the case study, then click **Upload and split into
   clips**.
4. When it has finished splitting, **Open** the session. You see a timeline
   with one clip per student.
   - Check the clip boundaries. You can drag them, add or remove cuts, and
     rename clips (for example with student names).
   - Click **Export clips** and wait until the clips are ready.
5. In **Clip assessments**, either:
   - click **Run assessment** on one clip; or
   - tick several clips and click **Run Selected Assessments**.
6. Each clip gets its own progress bar. Open a clip's result when it says
   **Completed**.

---

## 7. Reading the results

When you **Open** a completed session:

| Tab | What it shows |
|---|---|
| **Transcript** | Everything that was said, who said it, and when. Click a time to jump the video there |
| **Content Scores** | Each checklist item: Yes / No, the evidence the AI found, and the overall pass or fail |
| **Communication Scores** | Each communication criterion (None / Some / Most / All), with evidence |
| **Feedback** | What the student should keep doing, start doing and stop doing |

- **Download Score Sheet** saves the scores as a spreadsheet (CSV, which opens
  in Excel).
- **Analytics** (top of the page) compares results across students and
  sessions.
- The AI marks are a **support for the examiner, not a replacement**. Check
  the evidence before relying on a mark.

---

## 8. If something goes wrong

| What you see | What to do |
|---|---|
| A session says **Failed** | Click **Re-run** on its card. If it fails again, note the session name and contact your technical contact |
| A session stays on **Queued** for more than 10 minutes | Check the **WORKER** window is still on the taskbar. If it isn't, double-click **OSCE AI Marker** again |
| The browser can't reach the page | Double-click **OSCE AI Marker** again; wait one minute; press F5 |
| You closed one of the two small windows by accident | Double-click **OSCE AI Marker** again. It restarts only what is missing; nothing is lost |
| The computer was restarted during marking | Start the program again. Unfinished work carries on, or shows **Re-run** |
| Scoring fails with a message about a key | **Settings → Provider API keys → Test.** The key may have expired; paste a new one |
| A red window says it could not start | Photo or screenshot of the window → send it to your technical contact |

**When you contact your technical contact, send:**
- a screenshot of the problem;
- the session name;
- roughly what time it happened.

---

## 9. Finishing for the day

1. Double-click **Stop OSCE AI Marker** on the Desktop.
2. Wait until it says it has stopped (a few seconds).
3. You can now shut the computer down.

Nothing is deleted when you stop. All sessions and results are there the next
time you start. Please **don't stop the program while videos are still
uploading**. Stopping during *marking* is fine: the marking continues after
the next start.

---

## 10. Do's and don'ts

**Do:**
- Keep the computer plugged in, and stop it going to sleep while videos are
  being marked.
- Change your password after your first sign-in.
- Tell your technical contact before deleting anything large from this
  computer.

**Don't:**
- Close the **WEBSITE** or **WORKER** windows while marking is running.
- Quit **Docker Desktop** (the whale icon near the clock). The program needs
  it.
- Move or rename the `C:\OSCE` folder.
- Share your password or the AI keys.
