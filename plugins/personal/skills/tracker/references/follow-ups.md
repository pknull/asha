# Follow-ups

Two directions, both from the last 30 days of mail unless the Keeper sets
another window.

- **Waiting on others:** mail the Keeper sent with no reply after the
  threshold, 4 days by default. No reply is fact; that a reply is expected is
  inferred unless the message asked a question or set a date.
- **Owed by the Keeper:** direct requests not yet answered, pending calendar
  invites, and promises in sent mail ("I'll send it Friday"). All inferred
  except invites.

Skip automated senders and recipients (no-reply addresses, receipts,
notifications, mailing lists), mail to the Keeper's own addresses, and sent
messages that close a conversation (thanks, acknowledgements).

## Gmail

Waiting on others:

1. Sent messages:
   `gws gmail users messages list --params '{"userId":"me","q":"in:sent newer_than:30d","maxResults":100}'`,
   following `nextPageToken` until it is absent.
2. For each distinct `threadId`:
   `gws gmail users threads get --params '{"userId":"me","id":"THREAD","format":"metadata","metadataHeaders":["From","To","Cc","Subject","Date"]}'`.
3. The thread waits on others when its last message carries the `SENT` label
   and is older than the threshold.

Owed by the Keeper:

- Unanswered requests: inbox threads from the window, excluding the
  promotions, social, updates and forums categories, whose last message is
  from someone else with the Keeper in `To` (not only `Cc`), and whose snippet
  or body asks a question or requests something. Read the body only when the
  snippet is ambiguous.
- Promises: sent messages from the last 14 days whose snippet or body commits
  the Keeper to something with a time. Read the body when the snippet is cut
  off mid-commitment.

## Proton

Resolve mailbox names from `mailboxes` by flag: Sent carries `\Sent`, the
all-mail view carries `\All`.

Waiting on others: Bridge exposes no threads. For each message from
`search --mailbox <Sent> --since <window start>`, search the all-mail mailbox
for a later message from any of its recipients with the same subject (strip
`Re:` and `Fwd:` prefixes): `search --mailbox <All> --from RECIPIENT
--subject "SUBJECT" --since <sent date>`. No match after the threshold means
waiting. A subject match is a heuristic; say so when a match decides an item.

Owed by the Keeper: messages in the INBOX that ask something or carry a due
date; the Keeper may keep the INBOX as an action list, in which case every
message there is owed. Promises in sent mail as for Gmail, using `read`.

Reads never mark mail read; both helpers peek.

## Invites

Pending invites: `gws calendar events list` over the next 14 days with
`singleEvents: true`, keeping events whose attendee entry with `self: true`
has `responseStatus` `needsAction`.

To respond, on the Keeper's word for that invite:

1. `gws calendar events get` the event; keep its full `attendees` array.
2. Change `responseStatus` on the `self: true` entry only.
3. `gws calendar events patch --params '{"calendarId":"CAL","eventId":"ID","sendUpdates":"all"}' --json '{"attendees":[...]}'`
   with the whole modified array, since a patch replaces the list.
4. Get the event again and confirm the attendee count is unchanged and the
   Keeper's status is the new one before reporting it.

## Follow-up tasks

Offer to track an item; create the task only on the Keeper's word.

- Content: `Follow up: <person> re <subject>` for waiting items, `Reply:
  <person> re <subject>` or `Do: <promise>` for owed items.
- Label `follow-up`. Check `GET /api/v1/labels` first and create the label
  if it is absent.
- Due date: the threshold date for waiting items, the promised or requested
  date for owed items, otherwise none.
- Description: the source and a stable reference: `gmail thread <threadId>`,
  or `proton <Message-ID>` (from `read`), falling back to `proton <mailbox>
  <uidvalidity>/<uid>, <date>`.

Before offering an item, check that no open task labelled `follow-up` and no
task completed in the last 30 days (`GET
/api/v1/tasks/completed/by_completion_date?since=...&until=...`, which returns
its tasks under `items`, not `results`) carries its reference. A tracked
item is never offered again.

**Dismissal:** when the Keeper says an item needs no follow-up, create its
task and close it at once with `dismissed` added to the description. The task
manager stays the one record, and the item is not raised again.
