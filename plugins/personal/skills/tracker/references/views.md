# Views

Each view gathers the sources in `SKILL.md`, ranks by its rules, and ends on
the one question it names. Ask that question once; the Keeper may skip it.

## Today

Horizon: now to the end of tomorrow.

1. Weather, one line, when configured.
2. Calendar: the rest of today's fixed events with times, then tomorrow's
   first commitment. Pending invites for the next 7 days go to owed items.
3. Tasks: overdue, due today, then undated priority 1 tasks. Count tasks due
   tomorrow without listing them.
4. Mail, in two parts:
   - Owed: unanswered direct requests and bills or deadlines with a stated
     date, newest deadline first. Gmail: unread and inbox threads from the last
     7 days, excluding promotions, social, updates and forums categories.
     Proton: the INBOX.
   - Follow-ups due: from `follow-ups.md`, only the items past their threshold.
   Summarise the rest of the unread mail as one count by kind (newsletters,
   notifications, personal), not a list.
5. Open time: what the fixed events leave of the day, and whether today's due
   items fit in it.

End: "What's the one thing that would make today a success?"

## Close

Horizon: today, and tomorrow morning.

1. Done today: tasks completed today (`GET
   /api/v1/tasks/completed/by_completion_date` with `since` and `until`
   bounding today; tasks come back under `items`) and events that happened.
2. Carrying over: tasks due today still open. Offer to reschedule each one
   the Keeper names; do not reschedule in bulk unasked.
3. Loose ends: new mail since morning that asks something of the Keeper, sent
   mail from today that promised something, follow-ups opened today.
4. Tomorrow: the first fixed event and the tasks due. Name anything that needs
   preparing tonight.

End: "Anything to capture before you stop?" Record what the Keeper names as
tasks only on their word.

## Week

Horizon: the past 7 days, then the next 7. Run as a conversation, one step at
a time, waiting for the Keeper between steps.

1. **Collect.** Inbox state in each mail source (count of open items), tasks
   with no due date added in the past week, and any commitment the Keeper
   remembers that is not captured yet.
2. **Look back.** Completed tasks over 7 days, the past week's calendar, and
   who the Keeper corresponded with most. Name threads still open.
3. **Open loops.** For each overdue or stale task (no activity in 30 days),
   ask: still wanted? If yes, what is the next concrete action, and is it a
   project of several steps? If no, offer to complete it, or to move it to
   a someday project when one exists. One task per question, oldest first, until the Keeper stops.
4. **Waiting.** All follow-ups in both directions, from `follow-ups.md`,
   including ones below threshold.
5. **Learnings.** What went well, what frustrated, what to do differently.
   Ask, do not suggest answers.
6. **Next week.** The next 7 days of calendar with heavy days named, tasks due,
   and deadlines. Ask for three outcomes: one that must happen, one that
   should, one bonus. Suggest putting deep work in the open blocks on light
   days and admin on heavy ones.

End: offer to set due dates or priorities for the three outcomes in the task
manager.

## Weekend reset

When the Keeper asks for it at the end of the week, run Week steps 1, 3 and 6
only.
