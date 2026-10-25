// The words of the core plugin pm's pages in English, as the hub's web had them.
evo.messages("en", {
  "sub": "Report for {date}. What was done is the working day before: {previous}.",
  "day": "Day",
  "today": "Today",
  "count": "{count, plural, one {# item} other {# items}}",
  "hidden": "{count, plural, one {# more item you may not see is counted here.} other {# more items you may not see are counted here.}}",
  "bareTitle": "This project has no task to report yet",
  "bareText": "The report fills in once tasks exist, and once a sprint is active. Add a task or start a sprint on the board.",
  "openBoard": "Open the board",
  "notReady": {
    "title": "The daily report is not ready",
    "text": "It reads the project's tasks and its pm config. Enable the Tasks plugin, or fix the pm config named below.",
    "tasks": "Tasks plugin",
    "config": "pm config"
  },
  "done": {
    "title": "Done on {day}",
    "empty": "Nothing moved to done that day."
  },
  "sprint": {
    "title": "Active sprint",
    "empty": "No sprint is active.",
    "done": "Done",
    "time": "Time gone",
    "scope": "Tasks at opening",
    "changed": "Since opening",
    "changedValue": "+{added} added, -{removed} removed",
    "useCases": "Use cases of the sprint",
    "ratio": "{done} of {total} done"
  },
  "late": {
    "title": "Late and at risk",
    "empty": "Nothing is late.",
    "overdue": "Due {due}, {days, plural, one {# working day} other {# working days}} late",
    "atRisk": "Due {due}, {days, plural, one {# working day} other {# working days}} left"
  },
  "review": {
    "title": "In review",
    "empty": "Nothing waits for review.",
    "age": "{days, plural, =0 {since today} one {# working day in review} other {# working days in review}}",
    "qa": "QA {who}"
  },
  "stale": {
    "title": "Stale",
    "empty": "No task in progress has gone quiet.",
    "idle": "No activity for {days, plural, one {# working day} other {# working days}}"
  },
  "blocked": {
    "title": "Blocked",
    "empty": "Nothing is blocked.",
    "byDecision": "Open decision",
    "byHand": "Set by hand",
    "waitingMember": "Waiting on",
    "waitingTask": "Waiting on task",
    "waitingText": "Waiting on",
    "age": "{days, plural, =0 {since today} one {# working day} other {# working days}}"
  },
  "escalate": {
    "title": "To escalate",
    "empty": "No blocker is old enough to escalate."
  },
  "load": {
    "title": "Load per member",
    "empty": "No member has write access.",
    "overloaded": "Overloaded",
    "free": "Free",
    "counts": "{doing} in progress of {limit}, {review} to review as QA, {late} late"
  },
  "common": {
    "retry": "Try again"
  }
});
