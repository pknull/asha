---
description: "Get daily overview: weather, horoscope, schedule, todos, important emails, Hacker News"
---

Provide my daily brief for today:

1. Read `Memory/creatorProfile.md` (if exists) or use defaults:
   - Location: Austin, TX (for weather)
   - Zodiac sign: Pisces (for horoscope)

2. Generate **Philosophical Reflection** - A contemplative paragraph (3-5 sentences):
   - Draw from cosmic horror themes (ontological instability, boundary dissolution, existential questions)
   - Link philosophy to daily practice/lived experience
   - Vary themes session-to-session: time as perception, chaos as structure, identity as process, coherence vs noise, resonance vs control
   - Tone: Contemplative and exploratory, not prescriptive. Question-driven when appropriate

3. Fetch and present (use fallback methods if primary fails):
   - **Weather** for location
     - Primary: Google Search (`weather [location] today`)
     - Fallback: WebFetch from weather.gov forecast page
   - **Horoscope** for zodiac sign today
     - Primary: Google Search (`[zodiac] horoscope today`)
     - Fallback: WebFetch from astrology.com
   - **Calendar events** for today (use mcp__google-workspace__get_events)
   - **Todos** for today including overdue (use mcp__todoist-ai__find-tasks-by-date)
   - **Important emails** - unread from past 24 hours (use mcp__google-workspace__search_gmail_messages with `is:unread is:important`)
   - **Hacker News** - Top 5 stories from front page
     - WebFetch `https://news.ycombinator.com/` and extract top headlines with points/comments

Present in clean, concise format optimized for morning review. If any section fails completely after fallback attempts, note unavailability without dumping to user.
