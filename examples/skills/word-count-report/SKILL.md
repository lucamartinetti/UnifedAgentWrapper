---
name: word-count-report
description: Produce a word-count report for text files in the workspace. Use when the user asks for word counts, text statistics, or a word-count report.
license: Apache-2.0
metadata:
  version: "1.0"
---

# Word count report

Produce a report of word counts for text files.

1. Identify the text files in scope (the user's request or, by default, `*.txt` and `*.md` in the workspace root).
2. For each file, count its words (whitespace-separated tokens). Use a shell command such as `wc -w <file>` when shell access is available; otherwise read the file and count.
3. Output a markdown table with one row per file: `| file | words |`, followed by a final row with the total.
4. End the report with the exact line: `WORD-COUNT-REPORT COMPLETE`.

Keep the report short — no commentary beyond the table and the closing line.
